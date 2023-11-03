# coding=utf-8
# Copyright 2023 HuggingFace Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import gc
import unittest

import tempfile
import numpy as np
import torch

from ..test_pipelines_common import to_np
from diffusers import AutoencoderKL, DDIMScheduler, PixArtAlphaPipeline, DPMSolverMultistepScheduler, Transformer2DModel
from diffusers.utils import is_xformers_available
from diffusers.utils.testing_utils import enable_full_determinism, load_numpy, nightly, require_torch_gpu, torch_device
from transformers import AutoTokenizer, T5EncoderModel

from ..test_pipelines_common import PipelineTesterMixin
from ..pipeline_params import TEXT_TO_IMAGE_BATCH_PARAMS, TEXT_TO_IMAGE_IMAGE_PARAMS, TEXT_TO_IMAGE_PARAMS


enable_full_determinism()


class PixArtAlphaPipelineFastTests(PipelineTesterMixin, unittest.TestCase):
    pipeline_class = PixArtAlphaPipeline
    params = TEXT_TO_IMAGE_PARAMS - {"cross_attention_kwargs"}
    batch_params = TEXT_TO_IMAGE_BATCH_PARAMS
    image_params = TEXT_TO_IMAGE_IMAGE_PARAMS
    image_latents_params = TEXT_TO_IMAGE_IMAGE_PARAMS

    required_optional_params = PipelineTesterMixin.required_optional_params

    def get_dummy_components(self):
        torch.manual_seed(0)
        transformer = Transformer2DModel(
            sample_size=16,
            num_layers=2,
            patch_size=2,
            attention_head_dim=8,
            num_attention_heads=3,
            caption_channels=32,
            in_channels=4,
            cross_attention_dim=24,
            out_channels=8,
            attention_bias=True,
            activation_fn="gelu-approximate",
            num_embeds_ada_norm=1000,
            norm_type="ada_norm_single",
            norm_elementwise_affine=False,
            output_type="pixart_dit",
        )
        vae = AutoencoderKL()
        scheduler = DDIMScheduler()
        text_encoder = T5EncoderModel.from_pretrained("hf-internal-testing/tiny-random-t5")

        tokenizer = AutoTokenizer.from_pretrained("hf-internal-testing/tiny-random-t5")

        components = {"transformer": transformer.eval(), "vae": vae.eval(), "scheduler": scheduler, "text_encoder": text_encoder, "tokenizer": tokenizer}
        return components

    def get_dummy_inputs(self, device, seed=0):
        if str(device).startswith("mps"):
            generator = torch.manual_seed(seed)
        else:
            generator = torch.Generator(device=device).manual_seed(seed)
        inputs = {
            "prompt": "A painting of a squirrel eating a burger",
            "generator": generator,
            "num_inference_steps": 2,
            "guidance_scale": 6.0,
            "output_type": "numpy",
            "height": 32,
            "width": 32,
        }
        return inputs

    def test_save_load_optional_components(self):
        components = self.get_dummy_components()
        pipe = self.pipeline_class(**components)
        pipe.to(torch_device)
        pipe.set_progress_bar_config(disable=None)

        inputs = self.get_dummy_inputs(torch_device)

        prompt = inputs["prompt"]
        generator = inputs["generator"]
        num_inference_steps = inputs["num_inference_steps"]
        output_type = inputs["output_type"]

        prompt_embeds, negative_prompt_embeds = pipe.encode_prompt(prompt)

        # inputs with prompt converted to embeddings
        inputs = {
            "prompt_embeds": prompt_embeds,
            "negative_prompt_embeds": negative_prompt_embeds,
            "generator": generator,
            "num_inference_steps": num_inference_steps,
            "output_type": output_type,
        }

        # set all optional components to None
        for optional_component in pipe._optional_components:
            setattr(pipe, optional_component, None)

        output = pipe(**inputs)[0]

        with tempfile.TemporaryDirectory() as tmpdir:
            pipe.save_pretrained(tmpdir)
            pipe_loaded = self.pipeline_class.from_pretrained(tmpdir)
            pipe_loaded.to(torch_device)
            pipe_loaded.set_progress_bar_config(disable=None)

        for optional_component in pipe._optional_components:
            self.assertTrue(
                getattr(pipe_loaded, optional_component) is None,
                f"`{optional_component}` did not stay set to None after loading.",
            )

        inputs = self.get_dummy_inputs(torch_device)

        generator = inputs["generator"]
        num_inference_steps = inputs["num_inference_steps"]
        output_type = inputs["output_type"]

        # inputs with prompt converted to embeddings
        inputs = {
            "prompt_embeds": prompt_embeds,
            "negative_prompt_embeds": negative_prompt_embeds,
            "generator": generator,
            "num_inference_steps": num_inference_steps,
            "output_type": output_type,
        }

        output_loaded = pipe_loaded(**inputs)[0]

        max_diff = np.abs(to_np(output) - to_np(output_loaded)).max()
        self.assertLess(max_diff, 1e-4)

    def test_inference(self):
        device = "cpu"

        components = self.get_dummy_components()
        pipe = self.pipeline_class(**components)
        pipe.to(device)
        pipe.set_progress_bar_config(disable=None)

        inputs = self.get_dummy_inputs(device)
        image = pipe(**inputs).images
        image_slice = image[0, -3:, -3:, -1]

        self.assertEqual(image.shape, (1, 32, 32, 3))
        expected_slice = np.array([0.5174, 0.2495, 0.5566, 0.5259, 0.6054, 0.4732, 0.4416, 0.5192, 0.5264])
        max_diff = np.abs(image_slice.flatten() - expected_slice).max()
        self.assertLessEqual(max_diff, 1e-3)

    def test_inference_batch_single_identical(self):
        self._test_inference_batch_single_identical(expected_max_diff=1e-3)


@nightly
@require_torch_gpu
class PixArtAlphaPipelineIntegrationTests(unittest.TestCase):
    def tearDown(self):
        super().tearDown()
        gc.collect()
        torch.cuda.empty_cache()

    def test_dit_256(self):
        generator = torch.manual_seed(0)

        pipe = PixArtAlphaPipeline.from_pretrained("facebook/PixArtAlpha-XL-2-256")
        pipe.to("cuda")

        words = ["vase", "umbrella", "white shark", "white wolf"]
        ids = pipe.get_label_ids(words)

        images = pipe(ids, generator=generator, num_inference_steps=40, output_type="np").images

        for word, image in zip(words, images):
            expected_image = load_numpy(
                f"https://huggingface.co/datasets/hf-internal-testing/diffusers-images/resolve/main/dit/{word}.npy"
            )
            assert np.abs((expected_image - image).max()) < 1e-2

    def test_dit_512(self):
        pipe = PixArtAlphaPipeline.from_pretrained("facebook/PixArtAlpha-XL-2-512")
        pipe.scheduler = DPMSolverMultistepScheduler.from_config(pipe.scheduler.config)
        pipe.to("cuda")

        words = ["vase", "umbrella"]
        ids = pipe.get_label_ids(words)

        generator = torch.manual_seed(0)
        images = pipe(ids, generator=generator, num_inference_steps=25, output_type="np").images

        for word, image in zip(words, images):
            expected_image = load_numpy(
                "https://huggingface.co/datasets/hf-internal-testing/diffusers-images/resolve/main"
                f"/dit/{word}_512.npy"
            )

            assert np.abs((expected_image - image).max()) < 1e-1