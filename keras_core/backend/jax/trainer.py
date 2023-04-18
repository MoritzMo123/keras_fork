import jax
import numpy as np

from keras_core import backend
from keras_core import callbacks as callbacks_module
from keras_core import optimizers as optimizers_module
from keras_core.trainers import trainer as base_trainer
from keras_core.trainers.data_adapters import data_adapters_utils
from keras_core.trainers.epoch_iterator import EpochIterator


class Trainer(base_trainer.Trainer):

    def fit(
        self,
        x=None,
        y=None,
        batch_size=None,
        epochs=1,
        verbose="auto",
        callbacks=None,
        validation_split=0.0,
        validation_data=None,
        shuffle=True,
        class_weight=None,
        sample_weight=None,
        initial_epoch=0,
        steps_per_epoch=None,
        validation_steps=None,
        validation_batch_size=None,
        validation_freq=1,
    ):
        if not self.compiled:
            raise ValueError(
                "You must call `compile()` before calling `fit()`."
            )

        # TODO: respect compiled trainable state
        if validation_split and validation_data is None:
            # Create the validation data using the training data. Only supported
            # for TF/numpy/jax arrays.
            (
                x,
                y,
                sample_weight,
            ), validation_data = data_adapters_utils.train_validation_split(
                (x, y, sample_weight), validation_split=validation_split
            )

        if validation_data:
            (
                val_x,
                val_y,
                val_sample_weight,
            ) = data_adapters_utils.unpack_x_y_sample_weight(validation_data)

        # Create an iterator that yields batches for one epoch.
        epoch_iterator = EpochIterator(
            x=x,
            y=y,
            sample_weight=sample_weight,
            batch_size=batch_size,
            steps_per_epoch=steps_per_epoch,
            shuffle=shuffle,
            class_weight=class_weight,
        )

        if not self.built:
            # Build the model on one batch of data.
            for _, data in epoch_iterator.enumerate_epoch(return_type="np"):
                x, y, _ = data_adapters_utils.unpack_x_y_sample_weight(data)
                # Build model
                self(x)
                break

        # Container that configures and calls callbacks.
        if not isinstance(callbacks, callbacks_module.CallbackList):
            callbacks = callbacks_module.CallbackList(
                callbacks,
                add_history=True,
                add_progbar=verbose != 0,
                verbose=verbose,
                epochs=epochs,
                steps=epoch_iterator.num_batches,
                model=self,
            )

        self.stop_training = False
        # self.make_train_function()
        callbacks.on_train_begin()
        training_logs = None
        logs = None

        trainable_variables = self.trainable_variables
        if not self.optimizer.built:
            self.optimizer.build(trainable_variables)
        non_trainable_variables = self.non_trainable_variables
        optimizer_variables = self.optimizer.variables


        def compute_loss_and_updates(
            trainable_variables, non_trainable_variables, x, y
        ):
            y_pred, non_trainable_variables = self.stateless_call(
                trainable_variables, non_trainable_variables, x
            )
            
            loss = self._compile_loss(y, y_pred)
            return loss, (y_pred, non_trainable_variables)


        grad_fn = jax.value_and_grad(compute_loss_and_updates, has_aux=True)


        @jax.jit
        def train_step(
            trainable_variables, non_trainable_variables, optimizer_variables, x, y
        ):
            (loss, (y_pred, non_trainable_variables)), grads = grad_fn(
                trainable_variables, non_trainable_variables, x, y
            )
            # trainable_variables, optimizer_variables = self.optimizer.stateless_apply(
            #     grads, optimizer_variables
            # )
            new_trainable_variables = []
            for grad, var in zip(grads, trainable_variables):
                new_trainable_variables.append(var - 0.0001 * grad)
            trainable_variables = new_trainable_variables

            
            return loss, trainable_variables, non_trainable_variables, optimizer_variables

        # counter = jax.numpy.ones(())

        for epoch in range(initial_epoch, epochs):
            self.reset_metrics()
            callbacks.on_epoch_begin(epoch)
            for step, data in epoch_iterator.enumerate_epoch(return_type="np"):
                # Callbacks
                callbacks.on_train_batch_begin(step)

                first_var = trainable_variables[0]


                # Train step
                x, y, sample_weight = data_adapters_utils.unpack_x_y_sample_weight(data)
                (
                    loss,
                    trainable_variables,
                    non_trainable_variables,
                    optimizer_variables,
                ) = train_step(
                    trainable_variables, non_trainable_variables, optimizer_variables, x, y
                )

                self._loss_tracker.update_state(loss)

                # print(np.sum(np.abs(trainable_variables[0] - first_var)))

                # Callbacks
                callbacks.on_train_batch_end(step, logs)
                if self.stop_training:
                    break

            # Override with model metrics instead of last step logs
            epoch_logs = {"loss": self._loss_tracker.result()} # self._process_logs(self.get_metrics_result())

            # Run validation.
            if validation_data and self._should_eval(epoch, validation_freq):
                # Create EpochIterator for evaluation and cache it.
                if getattr(self, "_eval_epoch_iterator", None) is None:
                    self._eval_epoch_iterator = EpochIterator(
                        x=val_x,
                        y=val_y,
                        sample_weight=val_sample_weight,
                        batch_size=validation_batch_size or batch_size,
                        epochs=1,
                    )
                val_logs = self.evaluate(
                    x=val_x,
                    y=val_y,
                    sample_weight=val_sample_weight,
                    batch_size=validation_batch_size or batch_size,
                    steps=validation_steps,
                    callbacks=callbacks,
                    return_dict=True,
                    _use_cached_eval_dataset=True,
                )
                val_logs = {
                    "val_" + name: val for name, val in val_logs.items()
                }
                epoch_logs.update(self._process_logs(val_logs))

            callbacks.on_epoch_end(epoch, epoch_logs)
            training_logs = epoch_logs
            if self.stop_training:
                break

        if (
            isinstance(self.optimizer, optimizers_module.Optimizer)
            and epochs > 0
        ):
            self.optimizer.finalize_variable_values(self.trainable_weights)

        # If _eval_epoch_iterator exists, delete it after all epochs are done.
        if getattr(self, "_eval_epoch_iterator", None) is not None:
            del self._eval_epoch_iterator
        callbacks.on_train_end(logs=training_logs)
        return self.history

    def evaluate(
        self,
        x=None,
        y=None,
        batch_size=None,
        verbose="auto",
        sample_weight=None,
        steps=None,
        callbacks=None,
        return_dict=False,
        **kwargs,
    ):
        raise NotImplementedError

    def predict(
        self, x, batch_size=None, verbose="auto", steps=None, callbacks=None
    ):
        raise NotImplementedError
    
    def _process_logs(self, logs):
        result = {}
        for key, value in logs.items():
            try:
                value = float(value)
            except:
                pass
            result[key] = value
        return result