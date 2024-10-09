# Copyright 2023-present, Argilla, Inc.
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

import random
from itertools import islice
from typing import TYPE_CHECKING, Any, Dict, List

from pydantic import Field
from typing_extensions import override

from distilabel.steps.base import GeneratorStep

if TYPE_CHECKING:
    from distilabel.steps.base import GeneratorStepOutput


class DataSampler(GeneratorStep):
    """Step to sample from a dataset.

    `GeneratorStep` that samples from a dataset and yields it in batches.
    This step is useful when you have a pipeline that can benefit from using examples
    in the prompts for example as few-shot learning, that can be changing on each row.
    For example, you can pass a list of dictionaries with N examples and generate M samples
    from it (assuming you have another step loading data, this M should have the same size
    as the data being loaded in that step). The size S argument is the number of samples per
    row generated, so each example would contain S examples to be used as examples.

    Attributes:
        data: The list of dictionaries to sample from.
        size: Number of samples per example. For example in a few-shot learning scenario,
            the number of few-shot examples that will be generated per example. Defaults to 2.
        samples: Number of examples that will be generated by the step in total.
            If used with another loader step, this should be the same as the number
            of samples in the loader step. Defaults to 100.

    Output columns:
        - dynamic (based on the keys found on the first dictionary of the list): The columns
            of the dataset.

    Categories:
        - load

    Examples:
        Sample data from a list of dictionaries:

        ```python
        from distilabel.steps import DataSampler

        sampler = DataSampler(
            data=[{"sample": f"sample {i}"} for i in range(30)],
            samples=10,
            size=2,
            batch_size=4
        )
        sampler.load()

        result = next(sampler.process())
        # >>> result
        # ([{'sample': ['sample 7', 'sample 0']}, {'sample': ['sample 2', 'sample 21']}, {'sample': ['sample 17', 'sample 12']}, {'sample': ['sample 2', 'sample 14']}], False)
        ```

        Pipeline with a loader and a sampler combined in a single stream:

        ```python
        from datasets import load_dataset

        from distilabel.steps import LoadDataFromDicts, DataSampler
        from distilabel.steps.tasks.apigen.utils import PrepareExamples
        from distilabel.pipeline import Pipeline

        ds = (
            load_dataset("Salesforce/xlam-function-calling-60k", split="train")
            .shuffle(seed=42)
            .select(range(500))
            .to_list()
        )
        data = [
            {
                "func_name": "final_velocity",
                "func_desc": "Calculates the final velocity of an object given its initial velocity, acceleration, and time.",
            },
            {
                "func_name": "permutation_count",
                "func_desc": "Calculates the number of permutations of k elements from a set of n elements.",
            },
            {
                "func_name": "getdivision",
                "func_desc": "Divides two numbers by making an API call to a division service.",
            },
        ]
        with Pipeline(name="APIGenPipeline") as pipeline:
            loader_seeds = LoadDataFromDicts(data=data)
            sampler = DataSampler(
                data=ds,
                size=2,
                samples=len(data),
                batch_size=8,
            )
            prep_examples = PrepareExamples()

            sampler >> prep_examples
            (
                [loader_seeds, prep_examples]
                >> combine_steps
            )
        # Now we have a single stream of data with the loader and the sampler data
        ```
    """

    data: List[Dict[str, Any]] = Field(default_factory=list, exclude=True)
    size: int = Field(
        default=2,
        description=(
            "Number of samples per example. For example in a few-shot learning scenario, the number "
            "of few-shot examples that will be generated per example."
        ),
    )
    samples: int = Field(
        default=100,
        description=(
            "Number of examples that will be generated by the step in total. "
            "If used with another loader step, this should be the same as the number of "
            "samples in the loader step."
        ),
    )

    def model_post_init(self, __context: Any) -> None:
        super().model_post_init(__context)
        self.outputs = list(self.data[0].keys())

    @override
    def process(self, offset: int = 0) -> "GeneratorStepOutput":  # type: ignore
        """Yields batches from a list of dictionaries.

        Args:
            offset: The offset to start the generation from. Defaults to `0`.

        Yields:
            A list of Python dictionaries as read from the inputs (propagated in batches)
            and a flag indicating whether the yield batch is the last one.
        """

        total_samples = 0

        while total_samples < self.samples:
            batch = []
            bs = min(self.batch_size, self.samples - total_samples)
            for _ in range(self.batch_size):
                choices = random.choices(self.data, k=self.size)
                choices = self._transform_data(choices)
                batch.extend(choices)
            total_samples += bs
            batch = list(islice(batch, bs))
            yield (batch, True if total_samples >= self.samples else False)
            batch = []

    @staticmethod
    def _transform_data(data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not data:
            return []

        result = {key: [] for key in data[0].keys()}

        for item in data:
            for key, value in item.items():
                result[key].append(value)

        return [result]
