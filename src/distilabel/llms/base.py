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

import asyncio
import inspect
import json
import logging
import os
import sys
import time
from abc import ABC, abstractmethod
from functools import cached_property
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, Union

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from distilabel.constants import SIGINT_HANDLER_CALLED_ENV_NAME
from distilabel.errors import DistilabelNotImplementedError, DistilabelUserError
from distilabel.exceptions import DistilabelOfflineBatchGenerationNotFinishedException
from distilabel.mixins.runtime_parameters import (
    RuntimeParameter,
    RuntimeParametersMixin,
)
from distilabel.utils.docstring import parse_google_docstring
from distilabel.utils.notebook import in_notebook
from distilabel.utils.serialization import _Serializable

if TYPE_CHECKING:
    from logging import Logger

    from distilabel.llms.typing import GenerateOutput, HiddenState
    from distilabel.mixins.runtime_parameters import (
        RuntimeParameterInfo,
        RuntimeParametersNames,
    )
    from distilabel.steps.tasks.structured_outputs.outlines import StructuredOutputType
    from distilabel.steps.tasks.typing import (
        FormattedInput,
        InstructorStructuredOutputType,
        StandardInput,
    )
    from distilabel.utils.docstring import Docstring

if in_notebook():
    import nest_asyncio

    nest_asyncio.apply()


class LLM(RuntimeParametersMixin, BaseModel, _Serializable, ABC):
    """Base class for `LLM`s to be used in `distilabel` framework.

    To implement an `LLM` subclass, you need to subclass this class and implement:
        - `load` method to load the `LLM` if needed. Don't forget to call `super().load()`,
            so the `_logger` attribute is initialized.
        - `model_name` property to return the model name used for the LLM.
        - `generate` method to generate `num_generations` per input in `inputs`.

    Attributes:
        generation_kwargs: the kwargs to be propagated to either `generate` or `agenerate`
            methods within each `LLM`.
        use_offline_batch_generation: whether to use the `offline_batch_generate` method to
            generate the responses.
        offline_batch_generation_block_until_done: if provided, then polling will be done until
            the `ofline_batch_generate` method is able to retrieve the results. The value indicate
            the time to wait between each polling.
        jobs_ids: the job ids generated by the `offline_batch_generate` method. This attribute
            is used to store the job ids generated by the `offline_batch_generate` method
            so later they can be used to retrieve the results. It is not meant to be set by
            the user.
        _logger: the logger to be used for the `LLM`. It will be initialized when the `load`
            method is called.
    """

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        protected_namespaces=(),
        validate_default=True,
        validate_assignment=True,
        extra="forbid",
    )

    generation_kwargs: Optional[RuntimeParameter[Dict[str, Any]]] = Field(
        default_factory=dict,
        description="The kwargs to be propagated to either `generate` or `agenerate`"
        " methods within each `LLM`.",
    )
    use_offline_batch_generation: Optional[RuntimeParameter[bool]] = Field(
        default=False,
        description="Whether to use the `offline_batch_generate` method to generate"
        " the responses.",
    )
    offline_batch_generation_block_until_done: Optional[RuntimeParameter[int]] = Field(
        default=None,
        description="If provided, then polling will be done until the `ofline_batch_generate`"
        " method is able to retrieve the results. The value indicate the time to wait between"
        " each polling.",
    )

    jobs_ids: Union[Tuple[str, ...], None] = Field(default=None)
    _logger: "Logger" = PrivateAttr(None)

    def load(self) -> None:
        """Method to be called to initialize the `LLM`, its logger and optionally the
        structured output generator."""
        self._logger = logging.getLogger(f"distilabel.llm.{self.model_name}")

    def unload(self) -> None:
        """Method to be called to unload the `LLM` and release any resources."""
        pass

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Returns the model name used for the LLM."""
        pass

    def get_generation_kwargs(self) -> Dict[str, Any]:
        """Returns the generation kwargs to be used for the generation. This method can
        be overridden to provide a more complex logic for the generation kwargs.

        Returns:
            The kwargs to be used for the generation.
        """
        return self.generation_kwargs  # type: ignore

    @abstractmethod
    def generate(
        self,
        inputs: List["FormattedInput"],
        num_generations: int = 1,
        **kwargs: Any,
    ) -> List["GenerateOutput"]:
        """Abstract method to be implemented by each LLM to generate `num_generations`
        per input in `inputs`.

        Args:
            inputs: the list of inputs to generate responses for which follows OpenAI's
                API format:

                ```python
                [
                    {"role": "system", "content": "You're a helpful assistant..."},
                    {"role": "user", "content": "Give a template email for B2B communications..."},
                    {"role": "assistant", "content": "Sure, here's a template you can use..."},
                    {"role": "user", "content": "Modify the second paragraph..."}
                ]
                ```
            num_generations: the number of generations to generate per input.
            **kwargs: the additional kwargs to be used for the generation.
        """
        pass

    def generate_outputs(
        self,
        inputs: List["FormattedInput"],
        num_generations: int = 1,
        **kwargs: Any,
    ) -> List["GenerateOutput"]:
        """Generates outputs for the given inputs using either `generate` method or the
        `offine_batch_generate` method if `use_offline_
        """
        if self.use_offline_batch_generation:
            if self.offline_batch_generation_block_until_done is not None:
                return self._offline_batch_generate_polling(
                    inputs=inputs,
                    num_generations=num_generations,
                    **kwargs,
                )

            # This will raise `DistilabelOfflineBatchGenerationNotFinishedException` right away
            # if the batch generation is not finished.
            return self.offline_batch_generate(
                inputs=inputs,
                num_generations=num_generations,
                **kwargs,
            )

        return self.generate(inputs=inputs, num_generations=num_generations, **kwargs)

    def _offline_batch_generate_polling(
        self,
        inputs: List["FormattedInput"],
        num_generations: int = 1,
        **kwargs: Any,
    ) -> List["GenerateOutput"]:
        """Method to poll the `offline_batch_generate` method until the batch generation
        is finished.

        Args:
            inputs: the list of inputs to generate responses for.
            num_generations: the number of generations to generate per input.
            **kwargs: the additional kwargs to be used for the generation.

        Returns:
            A list containing the generations for each input.
        """
        while True:
            try:
                return self.offline_batch_generate(
                    inputs=inputs,
                    num_generations=num_generations,
                    **kwargs,
                )
            except DistilabelOfflineBatchGenerationNotFinishedException as e:
                self._logger.info(
                    f"Waiting for the offline batch generation to finish: {e}. Sleeping"
                    f" for {self.offline_batch_generation_block_until_done} seconds before"
                    " trying to get the results again."
                )
                # When running a `Step` in a child process, SIGINT is overriden so the child
                # process doesn't stop when the parent process receives a SIGINT signal.
                # The new handler sets an environment variable that is checked here to stop
                # the polling.
                if os.getenv(SIGINT_HANDLER_CALLED_ENV_NAME) is not None:
                    self._logger.info(
                        "Received a KeyboardInterrupt. Stopping polling for checking if the"
                        " offline batch generation is finished..."
                    )
                    raise e
                time.sleep(self.offline_batch_generation_block_until_done)  # type: ignore
            except KeyboardInterrupt as e:
                # This is for the case the `LLM` is being executed outside a pipeline
                self._logger.info(
                    "Received a KeyboardInterrupt. Stopping polling for checking if the"
                    " offline batch generation is finished..."
                )
                raise DistilabelOfflineBatchGenerationNotFinishedException(
                    jobs_ids=self.jobs_ids  # type: ignore
                ) from e

    @property
    def generate_parameters(self) -> List["inspect.Parameter"]:
        """Returns the parameters of the `generate` method.

        Returns:
            A list containing the parameters of the `generate` method.
        """
        return list(inspect.signature(self.generate).parameters.values())

    @property
    def runtime_parameters_names(self) -> "RuntimeParametersNames":
        """Returns the runtime parameters of the `LLM`, which are combination of the
        attributes of the `LLM` type hinted with `RuntimeParameter` and the parameters
        of the `generate` method that are not `input` and `num_generations`.

        Returns:
            A dictionary with the name of the runtime parameters as keys and a boolean
            indicating if the parameter is optional or not.
        """
        runtime_parameters = super().runtime_parameters_names
        runtime_parameters["generation_kwargs"] = {}

        # runtime parameters from the `generate` method
        for param in self.generate_parameters:
            if param.name in ["input", "inputs", "num_generations"]:
                continue
            is_optional = param.default != inspect.Parameter.empty
            runtime_parameters["generation_kwargs"][param.name] = is_optional

        return runtime_parameters

    def get_runtime_parameters_info(self) -> List["RuntimeParameterInfo"]:
        """Gets the information of the runtime parameters of the `LLM` such as the name
        and the description. This function is meant to include the information of the runtime
        parameters in the serialized data of the `LLM`.

        Returns:
            A list containing the information for each runtime parameter of the `LLM`.
        """
        runtime_parameters_info = super().get_runtime_parameters_info()

        generation_kwargs_info = next(
            (
                runtime_parameter_info
                for runtime_parameter_info in runtime_parameters_info
                if runtime_parameter_info["name"] == "generation_kwargs"
            ),
            None,
        )

        # If `generation_kwargs` attribute is present, we need to include the `generate`
        # method arguments as the information for this attribute.
        if generation_kwargs_info:
            generate_docstring_args = self.generate_parsed_docstring["args"]

            generation_kwargs_info["keys"] = []
            for key, value in generation_kwargs_info["optional"].items():
                info = {"name": key, "optional": value}
                if description := generate_docstring_args.get(key):
                    info["description"] = description
                generation_kwargs_info["keys"].append(info)

            generation_kwargs_info.pop("optional")

        return runtime_parameters_info

    @cached_property
    def generate_parsed_docstring(self) -> "Docstring":
        """Returns the parsed docstring of the `generate` method.

        Returns:
            The parsed docstring of the `generate` method.
        """
        return parse_google_docstring(self.generate)

    def get_last_hidden_states(
        self, inputs: List["StandardInput"]
    ) -> List["HiddenState"]:
        """Method to get the last hidden states of the model for a list of inputs.

        Args:
            inputs: the list of inputs to get the last hidden states from.

        Returns:
            A list containing the last hidden state for each sequence using a NumPy array
                with shape [num_tokens, hidden_size].
        """
        # TODO: update to use `DistilabelNotImplementedError`
        raise NotImplementedError(
            f"Method `get_last_hidden_states` is not implemented for `{self.__class__.__name__}`"
        )

    def _prepare_structured_output(
        self, structured_output: Optional["StructuredOutputType"] = None
    ) -> Union[Any, None]:
        """Method in charge of preparing the structured output generator.

        By default will raise a `NotImplementedError`, subclasses that allow it must override this
        method with the implementation.

        Args:
            structured_output: the config to prepare the guided generation.

        Returns:
            The structure to be used for the guided generation.
        """
        # TODO: update to use `DistilabelNotImplementedError`
        raise NotImplementedError(
            f"Guided generation is not implemented for `{type(self).__name__}`"
        )

    def offline_batch_generate(
        self,
        inputs: Union[List["FormattedInput"], None] = None,
        num_generations: int = 1,
        **kwargs: Any,
    ) -> List["GenerateOutput"]:
        """Method to generate a list of outputs for the given inputs using an offline batch
        generation method to be implemented by each `LLM`.

        This method should create jobs the first time is called and store the job ids, so
        the second and subsequent calls can retrieve the results of the batch generation.
        If subsequent calls are made before the batch generation is finished, then the method
        should raise a `DistilabelOfflineBatchGenerationNotFinishedException`. This exception
        will be handled automatically by the `Pipeline` which will store all the required
        information for recovering the pipeline execution when the batch generation is finished.

        Args:
            inputs: the list of inputs to generate responses for.
            num_generations: the number of generations to generate per input.
            **kwargs: the additional kwargs to be used for the generation.

        Returns:
            A list containing the generations for each input.
        """
        raise DistilabelNotImplementedError(
            f"`offline_batch_generate` is not implemented for `{self.__class__.__name__}`",
            page="sections/how_to_guides/advanced/offline-batch-generation/",
        )


class AsyncLLM(LLM):
    """Abstract class for asynchronous LLMs, so as to benefit from the async capabilities
    of each LLM implementation. This class is meant to be subclassed by each LLM, and the
    method `agenerate` needs to be implemented to provide the asynchronous generation of
    responses.

    Attributes:
        _event_loop: the event loop to be used for the asynchronous generation of responses.
    """

    _num_generations_param_supported = True
    _event_loop: "asyncio.AbstractEventLoop" = PrivateAttr(default=None)
    _new_event_loop: bool = PrivateAttr(default=False)

    @property
    def generate_parameters(self) -> List[inspect.Parameter]:
        """Returns the parameters of the `agenerate` method.

        Returns:
            A list containing the parameters of the `agenerate` method.
        """
        return list(inspect.signature(self.agenerate).parameters.values())

    @cached_property
    def generate_parsed_docstring(self) -> "Docstring":
        """Returns the parsed docstring of the `agenerate` method.

        Returns:
            The parsed docstring of the `agenerate` method.
        """
        return parse_google_docstring(self.agenerate)

    @property
    def event_loop(self) -> "asyncio.AbstractEventLoop":
        if self._event_loop is None:
            try:
                self._event_loop = asyncio.get_running_loop()
                if self._event_loop.is_closed():
                    self._event_loop = asyncio.new_event_loop()  # type: ignore
                    self._new_event_loop = True
            except RuntimeError:
                self._event_loop = asyncio.new_event_loop()
                self._new_event_loop = True
        asyncio.set_event_loop(self._event_loop)
        return self._event_loop

    @abstractmethod
    async def agenerate(
        self, input: "FormattedInput", num_generations: int = 1, **kwargs: Any
    ) -> List[Union[str, None]]:
        """Method to generate a `num_generations` responses for a given input asynchronously,
        and executed concurrently in `generate` method.
        """
        pass

    async def _agenerate(
        self, inputs: List["FormattedInput"], num_generations: int = 1, **kwargs: Any
    ) -> List["GenerateOutput"]:
        """Internal function to concurrently generate responses for a list of inputs.

        Args:
            inputs: the list of inputs to generate responses for.
            num_generations: the number of generations to generate per input.
            **kwargs: the additional kwargs to be used for the generation.

        Returns:
            A list containing the generations for each input.
        """
        if self._num_generations_param_supported:
            tasks = [
                asyncio.create_task(
                    self.agenerate(
                        input=input, num_generations=num_generations, **kwargs
                    )
                )
                for input in inputs
            ]
            result = await asyncio.gather(*tasks)
            return merge_responses(result)

        tasks = [
            asyncio.create_task(self.agenerate(input=input, **kwargs))
            for input in inputs
            for _ in range(num_generations)
        ]
        outputs = await asyncio.gather(*tasks)
        return merge_responses(outputs)

    def generate(
        self,
        inputs: List["FormattedInput"],
        num_generations: int = 1,
        **kwargs: Any,
    ) -> List["GenerateOutput"]:
        """Method to generate a list of responses asynchronously, returning the output
        synchronously awaiting for the response of each input sent to `agenerate`.

        Args:
            inputs: the list of inputs to generate responses for.
            num_generations: the number of generations to generate per input.
            **kwargs: the additional kwargs to be used for the generation.

        Returns:
            A list containing the generations for each input.
        """
        return self.event_loop.run_until_complete(
            self._agenerate(inputs=inputs, num_generations=num_generations, **kwargs)
        )

    def __del__(self) -> None:
        """Closes the event loop when the object is deleted."""
        if sys.meta_path is None:
            return

        if self._new_event_loop:
            if self._event_loop.is_running():
                self._event_loop.stop()
            self._event_loop.close()

    @staticmethod
    def _prepare_structured_output(  # type: ignore
        structured_output: "InstructorStructuredOutputType",
        client: Any = None,
        framework: Optional[str] = None,
    ) -> Dict[str, Union[str, Any]]:
        """Wraps the client and updates the schema to work store it internally as a json schema.

        Args:
            structured_output: The configuration dict to prepare the structured output.
            client: The client to wrap to generate structured output. Implemented to work
                with `instructor`.
            framework: The name of the framework.

        Returns:
            A dictionary containing the wrapped client and the schema to update the structured_output
            variable in case it is a pydantic model.
        """
        from distilabel.steps.tasks.structured_outputs.instructor import (
            prepare_instructor,
        )

        result = {}
        client = prepare_instructor(
            client,
            mode=structured_output.get("mode"),
            framework=framework,  # type: ignore
        )
        result["client"] = client

        schema = structured_output.get("schema")
        if not schema:
            raise DistilabelUserError(
                f"The `structured_output` argument must contain a schema: {structured_output}",
                page="sections/how_to_guides/advanced/structured_generation/#instructor",
            )
        if inspect.isclass(schema) and issubclass(schema, BaseModel):
            # We want a json schema for the serialization, but instructor wants a pydantic BaseModel.
            structured_output["schema"] = schema.model_json_schema()  # type: ignore
            result["structured_output"] = structured_output

        return result

    @staticmethod
    def _prepare_kwargs(
        arguments: Dict[str, Any], structured_output: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Helper method to update the kwargs with the structured output configuration,
        used in case they are defined.

        Args:
            arguments: The arguments that would be passed to the LLM as **kwargs.
                to update with the structured output configuration.
            structured_outputs: The structured output configuration to update the arguments.

        Returns:
            kwargs updated with the special arguments used by `instructor`.
        """
        # We can deal with json schema or BaseModel, but we need to convert it to a BaseModel
        # for the Instructor client.
        schema = structured_output.get("schema", {})

        # If there's already a pydantic model, we don't need to do anything,
        # otherwise, try to obtain one.
        if not (inspect.isclass(schema) and issubclass(schema, BaseModel)):
            from distilabel.steps.tasks.structured_outputs.utils import (
                json_schema_to_model,
            )

            if isinstance(schema, str):
                # In case it was saved in the dataset as a string.
                schema = json.loads(schema)

            try:
                schema = json_schema_to_model(schema)
            except Exception as e:
                raise ValueError(
                    f"Failed to convert the schema to a pydantic model, the model is too complex currently: {e}"
                ) from e

        arguments.update(
            **{
                "response_model": schema,
                "max_retries": structured_output.get("max_retries", 1),
            },
        )
        return arguments


def merge_responses(responses: List[Dict[str, Any]]) -> List["GenerateOutput"]:
    """Helper function to group the responses from `LLM.agenerate` method according
    to the number of generations requested.

    Args:
        responses: the responses from the `LLM.agenerate` method.

    Returns:
        Merges the texts and statistics of the responses into a single response.
    """
    if not responses:
        return []

    first = responses[0]
    return [
        {
            "generations": sum((r["generations"] for r in responses), []),
            "statistics": {
                key: sum((r["statistics"][key] for r in responses), [])
                for key in first["statistics"]
            },
        }
    ]
