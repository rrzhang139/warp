import numpy as np
import warp as wp
import warp.examples
import warp.optim

import torch as tc

import os

from PIL import Image

#wp.clear_kernel_cache()
#wp.config.mode = "debug"
#wp.config.verify_cuda = True

wp.set_module_options({"fast_math": False})

rng = np.random.default_rng(45)

def assert_equal(result: np.ndarray, expect: np.ndarray, tol=1.e-2):
    if tol != 0.0:
        # TODO: Get all tests working without the .flatten()
        np.testing.assert_allclose(result.flatten(), expect.flatten(), rtol=tol, atol=1.e-2, equal_nan=True)
    else:
        # TODO: Get all tests working with strict=True
        np.testing.assert_array_equal(result, expect)

    return True


def create_layer(dim_in, dim_hid, dtype=float):

    w = rng.uniform(-1.0 / np.sqrt(dim_in), 1.0 / np.sqrt(dim_in), (dim_hid, dim_in))
    b = rng.uniform(-1.0 / np.sqrt(dim_in), 1.0 / np.sqrt(dim_in), (dim_hid, 1))

    weights = wp.array(w, dtype=dtype, requires_grad=True)
    bias = wp.array(b, dtype=dtype, requires_grad=True)

    return (weights, bias)

def create_array(dim_in, dim_hid, dtype=float):

    s = rng.uniform(-1.0 / np.sqrt(dim_in), 1.0 / np.sqrt(dim_in), (dim_hid, dim_in))
    a = wp.array(s, dtype=dtype, requires_grad=True)

    return a


NUM_FREQ = wp.constant(4)

DIM_IN = wp.constant(4*NUM_FREQ)  # sin,cos for both x,y at each frequenecy
DIM_HID = 16
DIM_OUT = 3

NUM_THREADS = 32
NUM_BLOCKS = 36

IMG_WIDTH = NUM_THREADS*2
IMG_HEIGHT = NUM_THREADS*2

def test_multi_layer_nn():

    @wp.func
    def relu(x: float):
        return wp.max(x, 0.0)

    @wp.kernel
    def compute(input: wp.array2d(dtype=float),
                weights_0: wp.array2d(dtype=float), bias_0: wp.array2d(dtype=float),
                weights_1: wp.array2d(dtype=float), bias_1: wp.array2d(dtype=float),
                weights_2: wp.array2d(dtype=float), bias_2: wp.array2d(dtype=float),
                reference: wp.array2d(dtype=float),
                loss: wp.array1d(dtype=float),
                out: wp.array2d(dtype=float)):

        row, col = wp.tid()
        linear = row*IMG_WIDTH + col

        # normalize input coordinates to [-1, 1]
        x = (float(row)/float(IMG_WIDTH) - 0.5)*2.0
        y = (float(col)/float(IMG_HEIGHT) - 0.5)*2.0

        local = wp.vector(dtype=float, length=DIM_IN)

        # construct positional encoding
        for s in range(NUM_FREQ):

            scale = wp.pow(2.0, float(s))*wp.pi

            # x-coord
            local[s*4 + 0] = wp.sin(x * scale)
            local[s*4 + 1] = wp.cos(x * scale)

            # y-coord
            local[s*4 + 2] = wp.sin(y * scale)
            local[s*4 + 3] = wp.cos(y * scale)

            # write input back to array so that torch can use it
            input[s*4 + 0, linear] = local[s*4 + 0]
            input[s*4 + 1, linear] = local[s*4 + 1]
            input[s*4 + 2, linear] = local[s*4 + 2]
            input[s*4 + 3, linear] = local[s*4 + 3]
        

        # tile feature vectors across the block, returns [dim(f), NUM_THREADS]
        f = wp.tile(local)
        
        # input layer
        w0 = wp.tile_load(weights_0, 0, 0, m=DIM_HID, n=DIM_IN)
        b0 = wp.tile_load(bias_0, 0, 0, m=DIM_HID, n=1)
        z = wp.tile_map(relu, wp.tile_matmul(w0, f) + wp.tile_broadcast(b0, m=DIM_HID, n=NUM_THREADS))

        # hidden layer
        w1 = wp.tile_load(weights_1, 0, 0, m=DIM_HID, n=DIM_HID)
        b1 = wp.tile_load(bias_1, 0, 0, m=DIM_HID, n=1)
        z = wp.tile_map(relu, wp.tile_matmul(w1, z) + wp.tile_broadcast(b1, m=DIM_HID, n=NUM_THREADS))

        # output layer
        w2 = wp.tile_load(weights_2, 0, 0, m=DIM_OUT, n=DIM_HID)
        b2 = wp.tile_load(bias_2, 0, 0, m=DIM_OUT, n=1)
        o = wp.tile_map(relu, wp.tile_matmul(w2, z) + wp.tile_broadcast(b2, m=DIM_OUT, n=NUM_THREADS))

        # until back to SIMT
        output = wp.untile(o)

        # compute error
        error = wp.vec3(output[0] - reference[0,linear],
                        output[1] - reference[1,linear],
                        output[2] - reference[2,linear])

        # write MSE loss
        wp.atomic_add(loss, 0, wp.length_sq(error)/float(3*IMG_WIDTH*IMG_HEIGHT))

        # image output
        for i in range(DIM_OUT):
            out[i, linear] = output[i]
                


    weights_0, bias_0 = create_layer(DIM_IN, DIM_HID, dtype=float)
    weights_1, bias_1 = create_layer(DIM_HID, DIM_HID, dtype=float)
    weights_2, bias_2 = create_layer(DIM_HID, DIM_OUT, dtype=float)

    input = create_array(IMG_WIDTH*IMG_HEIGHT, DIM_IN)
    output = create_array(IMG_WIDTH*IMG_HEIGHT, DIM_OUT)

    # # reference 
    reference_path = os.path.join(wp.examples.get_asset_directory(), "pixel.jpg")
    with Image.open(reference_path) as im:
        reference_image = np.asarray(im.resize((IMG_WIDTH, IMG_HEIGHT)).convert("RGB")) / 255.0    
    reference = wp.array(reference_image.reshape(IMG_WIDTH*IMG_HEIGHT, 3).T, dtype=float)

    loss = wp.zeros(1, dtype=float, requires_grad=True)

    params = [weights_0, bias_0,
              weights_1, bias_1, 
              weights_2, bias_2]

    optimizer_grads = [p.grad.flatten() for p in params]
    optimizer_inputs = [p.flatten() for p in params]
    optimizer = warp.optim.Adam(optimizer_inputs, lr=0.001)

    for i in range(1):

        loss.zero_()

        with wp.Tape() as tape:
            wp.launch(
                compute, 
                dim=[IMG_WIDTH, IMG_HEIGHT], 
                inputs=[input,
                        weights_0, bias_0,
                        weights_1, bias_1,
                        weights_2, bias_2, 
                        reference,
                        loss,
                        output],
                block_dim=NUM_THREADS)

        print(f"Iter: {i} Loss: {loss.numpy()}")

        # output.grad = wp.ones_like(output)
        # tape.backward()
        
        tape.backward(loss)

        # optimizer.step(optimizer_grads)

        # tape.zero()


    predicted_image = output.numpy().T.reshape(IMG_WIDTH, IMG_HEIGHT, 3)
    predicted_image = (predicted_image * 255).astype(np.uint8)

    predicted_image_pil = Image.fromarray(predicted_image)
    predicted_image_pil.save("test_tile_mlp_wp.jpg")

    # print(input)
    # print(output)

    # numpy
    z_np = np.maximum(weights_0.numpy()@input.numpy() + bias_0.numpy(), 0.0)
    z_np = np.maximum(weights_1.numpy()@z_np + bias_1.numpy(), 0.0)
    z_np = np.maximum(weights_2.numpy()@z_np + bias_2.numpy(), 0.0)

    predicted_image = z_np.T.reshape(IMG_WIDTH, IMG_HEIGHT, 3)
    predicted_image = (predicted_image * 255).astype(np.uint8)

    predicted_image_pil = Image.fromarray(predicted_image)
    predicted_image_pil.save("test_tile_mlp_np.jpg")

    # test numpy foward
    print("NumPy output close: ", assert_equal(output.numpy(), z_np))

    # torch
    input_tc = tc.from_numpy(input.numpy()).requires_grad_(True)

    weights_0_tc = tc.from_numpy(weights_0.numpy()).requires_grad_(True)
    bias_0_tc = tc.from_numpy(bias_0.numpy()).requires_grad_(True)

    weights_1_tc = tc.from_numpy(weights_1.numpy()).requires_grad_(True)
    bias_1_tc = tc.from_numpy(bias_1.numpy()).requires_grad_(True)

    weights_2_tc = tc.from_numpy(weights_2.numpy()).requires_grad_(True)
    bias_2_tc = tc.from_numpy(bias_2.numpy()).requires_grad_(True)

    z_tc = tc.clamp(weights_0_tc@input_tc + bias_0_tc, min=0.0)
    z_tc = tc.clamp(weights_1_tc@z_tc + bias_1_tc, min=0.0)
    z_tc = tc.clamp(weights_2_tc@z_tc + bias_2_tc, min=0.0)
    
    ref_tc = tc.from_numpy(reference.numpy()).requires_grad_(True)
    
    
    l_tc = tc.mean((z_tc - ref_tc)**2)
    l_tc.backward()

    #z_tc.backward(tc.ones_like(z_tc))

    # test torch
    print("Torch output close:        ", assert_equal(z_tc.cpu().detach().numpy(), output.numpy()))
    #print("Torch loss close:        ", assert_equal(l_tc.cpu().detach().numpy(), loss.numpy()))
    #print("Torch input.grad close:    ", assert_equal(input.grad.numpy(), input_tc.grad.cpu().detach().numpy()))
     
    print("Torch weights0.grad close: ", assert_equal(weights_0.grad.numpy(), weights_0_tc.grad.cpu().detach().numpy()))
    print("Torch bias0.grad close:    ", assert_equal(bias_0.grad.numpy(), bias_0_tc.grad.cpu().detach().numpy()))
     
    print("Torch weights1.grad close: ", assert_equal(weights_1.grad.numpy(), weights_1_tc.grad.cpu().detach().numpy()))
    print("Torch bias1.grad close:    ", assert_equal(bias_1.grad.numpy(), bias_1_tc.grad.cpu().detach().numpy()))
 
    print("Torch weights2.grad close: ", assert_equal(weights_2.grad.numpy(), weights_2_tc.grad.cpu().detach().numpy()))
    print("Torch bias2.grad close:    ", assert_equal(bias_2.grad.numpy(), bias_2_tc.grad.cpu().detach().numpy()))

    




def test_single_layer_nn():

    @wp.func
    def relu(x: float):
        return wp.max(x, 0.0)

    @wp.kernel
    def compute(input: wp.array2d(dtype=float),
                weights: wp.array2d(dtype=float),
                bias: wp.array2d(dtype=float),
                out: wp.array2d(dtype=float)):

        i = wp.tid()

        f = wp.tile_load(input, 0, i, m=DIM_IN, n=NUM_THREADS)

        w = wp.tile_load(weights, 0, 0, DIM_OUT, DIM_IN)
        b = wp.tile_load(bias, 0, 0, m=DIM_OUT, n=1)

        o = wp.tile_map(relu, wp.tile_matmul(w, f) + wp.tile_broadcast(b, m=DIM_OUT, n=NUM_THREADS))

        wp.tile_store(out, 0, i, o)


    weights, bias = create_layer(DIM_IN, DIM_OUT, dtype=float)

    input = create_array(NUM_THREADS*NUM_BLOCKS, DIM_IN)
    output = create_array(NUM_THREADS*NUM_BLOCKS, DIM_OUT)

    with wp.Tape() as tape:
        wp.launch_tiled(compute, dim=[NUM_BLOCKS], inputs=[input, weights, bias, output], block_dim=NUM_THREADS)

    output.grad = wp.ones_like(output)
    tape.backward()    


    # print(input)
    # print(output)

    # numpy
    output_np = np.maximum(weights.numpy()@input.numpy() + bias.numpy(), 0.0)

    # test numpy foward
    print(np.allclose(output.numpy(), output_np))


    # torch
    weights_tc = tc.from_numpy(weights.numpy()).requires_grad_(True)   # use .numpy() to avoid any memory aliasing
    input_tc = tc.from_numpy(input.numpy()).requires_grad_(True)
    bias_tc = tc.from_numpy(bias.numpy()).requires_grad_(True)

    output_tc = tc.clamp(weights_tc@input_tc + bias_tc, min=0.0)
    output_tc.backward(tc.ones_like(output_tc))

    # test torch
    print(np.allclose(output_tc.detach().numpy(), output.numpy()))
    print(np.allclose(input.grad.numpy(), input_tc.grad.detach().numpy()))


#test_single_layer_nn()
test_multi_layer_nn()