# Copyright (c) MONAI Consortium
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import warnings
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union

import torch
from monai.config import IgniteInfo
from monai.engines.trainer import Trainer
from monai.engines.utils import CommonKeys as Keys
from monai.engines.utils import default_metric_cmp_fn, default_prepare_batch
from monai.inferers import Inferer, SimpleInferer
from monai.transforms import Transform
from monai.utils import min_version, optional_import
from torch.optim.optimizer import Optimizer
from torch.utils.data import DataLoader

from generative.utils import AdversarialIterationEvents, AdversarialKeys

if TYPE_CHECKING:
    from ignite.engine import EventEnum
    from ignite.metrics import Metric
else:
    Engine, _ = optional_import("ignite.engine", IgniteInfo.OPT_IMPORT_VERSION, min_version, "Engine")
    Metric, _ = optional_import("ignite.metrics", IgniteInfo.OPT_IMPORT_VERSION, min_version, "Metric")
    EventEnum, _ = optional_import("ignite.engine", IgniteInfo.OPT_IMPORT_VERSION, min_version, "EventEnum")

__all__ = ["AdversarialTrainer"]


class AdversarialTrainer(Trainer):
    """
    Standard supervised training workflow for adversarial loss enabled neural networks.

    Args:
        device: an object representing the device on which to run.
        max_epochs: the total epoch number for engine to run.
        train_data_loader: Core ignite engines uses `DataLoader` for training loop batchdata.
        g_network: ''generator'' (G) network architecture.
        g_optimizer: G optimizer function.
        g_loss_function: G loss function for adversarial training.
        recon_loss_function: G loss function for reconstructions.
        d_network: discriminator (D) network architecture.
        d_optimizer: D optimizer function.
        d_loss_function: D loss function for adversarial training..
        epoch_length: number of iterations for one epoch, default to `len(train_data_loader)`.
        non_blocking: if True and this copy is between CPU and GPU, the copy may occur asynchronously with respect to
            the host. For other cases, this argument has no effect.
        prepare_batch: function to parse image and label for current iteration.
        iteration_update: the callable function for every iteration, expect to accept `engine` and `batchdata` as input
            parameters. if not provided, use `self._iteration()` instead.
        g_inferer: inference method to execute G model forward. Defaults to ``SimpleInferer()``.
        d_inferer: inference method to execute D model forward. Defaults to ``SimpleInferer()``.
        postprocessing: execute additional transformation for the model output data. Typically, several Tensor based
            transforms composed by `Compose`. Defaults to None
        key_train_metric: compute metric when every iteration completed, and save average value to engine.state.metrics
            when epoch completed. key_train_metric is the main metric to compare and save the checkpoint into files.
        additional_metrics: more Ignite metrics that also attach to Ignite Engine.
        metric_cmp_fn: function to compare current key metric with previous best key metric value, it must accept 2 args
            (current_metric, previous_best) and return a bool result: if `True`, will update 'best_metric` and
            `best_metric_epoch` with current metric and epoch, default to `greater than`.
        train_handlers: every handler is a set of Ignite Event-Handlers, must have `attach` function, like:
            CheckpointHandler, StatsHandler, etc.
        amp: whether to enable auto-mixed-precision training, default is False.
        event_names: additional custom ignite events that will register to the engine.
            new events can be a list of str or `ignite.engine.events.EventEnum`.
        event_to_attr: a dictionary to map an event to a state attribute, then add to `engine.state`.
            for more details, check: https://pytorch.org/ignite/generated/ignite.engine.engine.Engine.html
            #ignite.engine.engine.Engine.register_events.
        decollate: whether to decollate the batch-first data to a list of data after model computation, recommend
            `decollate=True` when `postprocessing` uses components from `monai.transforms`. default to `True`.
        optim_set_to_none: when calling `optimizer.zero_grad()`, instead of setting to zero, set the grads to None.
            more details: https://pytorch.org/docs/stable/generated/torch.optim.Optimizer.zero_grad.html.
        to_kwargs: dict of other args for `prepare_batch` API when converting the input data, except for
            `device`, `non_blocking`.
        amp_kwargs: dict of the args for `torch.cuda.amp.autocast()` API, for more details:
            https://pytorch.org/docs/stable/amp.html#torch.cuda.amp.autocast.
    """

    def __init__(
        self,
        device: Union[torch.device, str],
        max_epochs: Union[int, None],
        train_data_loader: Union[Iterable, DataLoader],
        g_network: torch.nn.Module,
        g_optimizer: Optimizer,
        g_loss_function: Callable,
        recon_loss_function: Callable,
        d_network: torch.nn.Module,
        d_optimizer: Optimizer,
        d_loss_function: Callable,
        epoch_length: Optional[int] = None,
        non_blocking: bool = False,
        prepare_batch: Union[Callable[[Engine, Any], Any], None] = default_prepare_batch,
        iteration_update: Optional[Callable] = None,
        g_inferer: Optional[Inferer] = None,
        d_inferer: Optional[Inferer] = None,
        postprocessing: Optional[Transform] = None,
        key_train_metric: Optional[Dict[str, Metric]] = None,
        additional_metrics: Optional[Dict[str, Metric]] = None,
        metric_cmp_fn: Callable = default_metric_cmp_fn,
        train_handlers: Optional[Sequence] = None,
        amp: bool = False,
        event_names: Union[List[Union[str, EventEnum]], None] = None,
        event_to_attr: Union[dict, None] = None,
        decollate: bool = True,
        optim_set_to_none: bool = False,
        to_kwargs: Union[dict, None] = None,
        amp_kwargs: Union[dict, None] = None,
    ):
        super().__init__(
            device=device,
            max_epochs=max_epochs,
            data_loader=train_data_loader,
            epoch_length=epoch_length,
            non_blocking=non_blocking,
            prepare_batch=prepare_batch,
            iteration_update=iteration_update,
            postprocessing=postprocessing,
            key_metric=key_train_metric,
            additional_metrics=additional_metrics,
            metric_cmp_fn=metric_cmp_fn,
            handlers=train_handlers,
            amp=amp,
            event_names=event_names,
            event_to_attr=event_to_attr,
            decollate=decollate,
            to_kwargs=to_kwargs,
            amp_kwargs=amp_kwargs,
        )

        self.register_events(*AdversarialIterationEvents)

        self.g_network = g_network
        self.g_optimizer = g_optimizer
        self.g_loss_function = g_loss_function
        self.recon_loss_function = recon_loss_function

        self.d_network = d_network
        self.d_optimizer = d_optimizer
        self.d_loss_function = d_loss_function

        self.g_inferer = SimpleInferer() if g_inferer is None else g_inferer
        self.d_inferer = SimpleInferer() if d_inferer is None else d_inferer

        self.g_scaler = torch.cuda.amp.GradScaler() if self.amp else None
        self.d_scaler = torch.cuda.amp.GradScaler() if self.amp else None

        self.optim_set_to_none = optim_set_to_none

    def _iteration(
        self, engine: AdversarialTrainer, batchdata: Dict[str, torch.Tensor]
    ) -> Dict[str, Union[torch.Tensor, int, float, bool]]:
        """
        Callback function for the Adversarial Training processing logic of 1 iteration in Ignite Engine.
        Return below items in a dictionary:
            - IMAGE: image Tensor data for model input, already moved to device.
            - LABEL: label Tensor data corresponding to the image, already moved to device. In case of Unsupervised
                Learning this is equal to IMAGE.
            - PRED: prediction result of model.
            - LOSS: loss value computed by loss functions of the generator (reconstruction and adversarial summed up).
            - AdversarialKeys.REALS: real images from the batch. Are the same as IMAGE.
            - AdversarialKeys.FAKES: fake images generated by the generator. Are the same as PRED.
            - AdversarialKeys.REAL_LOGITS: logits of the discriminator for the real images.
            - AdversarialKeys.FAKE_LOGITS: logits of the discriminator for the fake images.
            - AdversarialKeys.RECONSTRUCTION_LOSS: loss value computed by the reconstruction loss function.
            - AdversarialKeys.GENERATOR_LOSS: loss value computed by the generator loss function. It is the
                discriminator loss for the fake images. That is backpropagated through the generator only.
            - AdversarialKeys.DISCRIMINATOR_LOSS: loss value computed by the discriminator loss function. It is the
                discriminator loss for the real images and the fake images. That is backpropagated through the
                discriminator only.

        Args:
            engine: `AdversarialTrainer` to execute operation for an iteration.
            batchdata: input data for this iteration, usually can be dictionary or tuple of Tensor data.

        Raises:
            ValueError: must provide batch data for current iteration.

        """

        if batchdata is None:
            raise ValueError("Must provide batch data for current iteration.")
        batch = engine.prepare_batch(batchdata, engine.state.device, engine.non_blocking, **engine.to_kwargs)

        if len(batch) == 2:
            inputs, targets = batch
            args: Tuple = ()
            kwargs: Dict = {}
        else:
            inputs, targets, args, kwargs = batch

        engine.state.output = {Keys.IMAGE: inputs, Keys.LABEL: targets, AdversarialKeys.REALS: inputs}

        def _compute_generator_loss() -> None:
            # TODO: Have a callable functions that process the input to the networks/losses such that peculiar outputs
            #  are handled properly
            engine.state.output[AdversarialKeys.FAKES] = engine.g_inferer(inputs, engine.g_network, *args, **kwargs)
            engine.state.output[Keys.PRED] = engine.state.output[AdversarialKeys.FAKES]
            engine.fire_event(AdversarialIterationEvents.GENERATOR_FORWARD_COMPLETED)

            engine.state.output[AdversarialKeys.FAKE_LOGITS] = engine.d_inferer(
                engine.state.output[AdversarialKeys.FAKES].float().contiguous(), engine.d_network, *args, **kwargs
            )
            engine.fire_event(AdversarialIterationEvents.GENERATOR_DISCRIMINATOR_FORWARD_COMPLETED)

            engine.state.output[AdversarialKeys.RECONSTRUCTION_LOSS] = engine.recon_loss_function(
                engine.state.output[AdversarialKeys.FAKES], targets
            ).mean()
            engine.fire_event(AdversarialIterationEvents.RECONSTRUCTION_LOSS_COMPLETED)

            engine.state.output[AdversarialKeys.GENERATOR_LOSS] = engine.g_loss_function(
                engine.state.output[AdversarialKeys.FAKE_LOGITS]
            ).mean()
            engine.fire_event(AdversarialIterationEvents.GENERATOR_LOSS_COMPLETED)

        # Train Generator
        engine.g_network.train()
        engine.g_optimizer.zero_grad(set_to_none=engine.optim_set_to_none)

        if engine.amp and engine.g_scaler is not None:
            with torch.cuda.amp.autocast(**engine.amp_kwargs):
                _compute_generator_loss()

            engine.state.output[Keys.LOSS] = (
                engine.state.output[AdversarialKeys.RECONSTRUCTION_LOSS]
                + engine.state.output[AdversarialKeys.GENERATOR_LOSS]
            )
            engine.g_scaler.scale(engine.state.output[Keys.LOSS]).backward()
            engine.fire_event(AdversarialIterationEvents.GENERATOR_BACKWARD_COMPLETED)
            engine.g_scaler.step(engine.g_optimizer)
            engine.g_scaler.update()
        else:
            _compute_generator_loss()
            (
                engine.state.output[AdversarialKeys.RECONSTRUCTION_LOSS]
                + engine.state.output[AdversarialKeys.GENERATOR_LOSS]
            ).backward()
            engine.fire_event(AdversarialIterationEvents.GENERATOR_BACKWARD_COMPLETED)
            engine.g_optimizer.step()
        engine.fire_event(AdversarialIterationEvents.GENERATOR_MODEL_COMPLETED)

        def _compute_discriminator_loss() -> None:
            engine.state.output[AdversarialKeys.REAL_LOGITS] = engine.d_inferer(
                engine.state.output[AdversarialKeys.REALS].contiguous().detach(), engine.d_network, *args, **kwargs
            )
            engine.fire_event(AdversarialIterationEvents.DISCRIMINATOR_REALS_FORWARD_COMPLETED)

            engine.state.output[AdversarialKeys.FAKE_LOGITS] = engine.d_inferer(
                engine.state.output[AdversarialKeys.FAKES].contiguous().detach(), engine.d_network, *args, **kwargs
            )
            engine.fire_event(AdversarialIterationEvents.DISCRIMINATOR_FAKES_FORWARD_COMPLETED)

            engine.state.output[AdversarialKeys.DISCRIMINATOR_LOSS] = engine.d_loss_function(
                engine.state.output[AdversarialKeys.REAL_LOGITS], engine.state.output[AdversarialKeys.FAKE_LOGITS]
            ).mean()
            engine.fire_event(AdversarialIterationEvents.DISCRIMINATOR_LOSS_COMPLETED)

        # Train Discriminator
        engine.d_network.train()
        engine.d_network.zero_grad(set_to_none=engine.optim_set_to_none)

        if engine.amp and engine.d_scaler is not None:
            with torch.cuda.amp.autocast(**engine.amp_kwargs):
                _compute_discriminator_loss()

            engine.d_scaler.scale(engine.state.output[AdversarialKeys.DISCRIMINATOR_LOSS]).backward()
            engine.fire_event(AdversarialIterationEvents.DISCRIMINATOR_BACKWARD_COMPLETED)
            engine.d_scaler.step(engine.d_optimizer)
            engine.d_scaler.update()
        else:
            _compute_discriminator_loss()
            engine.state.output[AdversarialKeys.DISCRIMINATOR_LOSS].backward()
            engine.d_optimizer.step()

        return engine.state.output

    def get_state(
        self,
        include_engine: bool = True,
        include_generator: bool = True,
        include_generator_optimiser: bool = True,
        include_generator_scaler: bool = False,
        include_generator_loss: bool = True,
        include_discriminator: bool = True,
        include_discriminator_optimiser: bool = True,
        include_discriminator_scaler: bool = False,
        include_discriminator_loss: bool = True,
        additional_states: Optional[Dict[str, Dict]] = None,
    ):
        state_dict = {}

        if include_engine:
            state_dict["engine"] = self.state_dict()
        else:
            warnings.warn("Engine state not included in checkpoint. This might cause issues when resuming training.")

        if include_generator:
            state_dict["generator"] = self.g_network.state_dict()
        else:
            warnings.warn("Generator state not included in checkpoint. This might cause issues when resuming training.")

        if include_generator_optimiser:
            state_dict["generator_optimizer"] = self.g_optimizer.state_dict()
        else:
            warnings.warn(
                "Generator optimizer state not included in checkpoint. This might cause issues when resuming training."
            )

        if include_generator_scaler:
            if self.g_scaler is not None:
                state_dict["generator_scaler"] = self.g_scaler.state_dict()
            else:
                warnings.warn(
                    "Generator AMP scaler was required in checkpoint but not found due to AMP being disabled."
                )
        else:
            warnings.warn(
                "Generator AMP scaler state not included in checkpoint. This might cause issues when resuming training."
            )

        if include_generator_loss:
            g_loss_state_dict = getattr(self.g_loss_function, "state_dict", None)
            if callable(g_loss_state_dict):
                state_dict["generator_loss"] = self.g_loss_function.state_dict()
            else:
                warnings.warn(
                    "Generator loss does not have a state_dict method. Make sure this is the intended behaviour."
                )
        else:
            warnings.warn(
                "Generator loss function state not included in checkpoint. This might cause issues when resuming training."
            )

        if include_discriminator:
            state_dict["discriminator"] = self.d_network.state_dict()
        else:
            warnings.warn(
                "Discriminator state not included in checkpoint. This might cause issues when resuming training."
            )

        if include_discriminator_optimiser:
            state_dict["discriminator_optimizer"] = self.d_optimizer.state_dict()
        else:
            warnings.warn(
                "Discriminator optimizer state not included in checkpoint. This might cause issues when resuming training."
            )

        if include_discriminator_scaler:
            if self.d_scaler is not None:
                state_dict["discriminator_scaler"] = self.d_scaler.state_dict()
            else:
                warnings.warn(
                    "Discriminator AMP scaler was required in checkpoint but not found due to AMP being disabled."
                )
        else:
            warnings.warn(
                "Discriminator AMP scaler state not included in checkpoint. This might cause issues when resuming training."
            )

        if include_discriminator_loss:
            d_loss_state_dict = getattr(self.d_loss_function, "state_dict", None)
            if callable(d_loss_state_dict):
                state_dict["discriminator_loss"] = self.d_loss_function.state_dict()
            else:
                warnings.warn(
                    "Discriminator loss does not have a state_dict method. Make sure this is the intended behaviour."
                )
        else:
            warnings.warn(
                "Discriminator loss function state not included in checkpoint. This might cause issues when resuming training."
            )

        if additional_states is not None:
            state_dict.update(additional_states)

        return state_dict
