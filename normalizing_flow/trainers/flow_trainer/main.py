import os
from torch.utils.tensorboard import SummaryWriter
import json
from torch.utils.data import DataLoader
import jax
import jax.numpy as jnp
from flax import linen as nn
import numpy as np
import optax
from tqdm import tqdm
from flax.training import train_state, checkpoints
import time


class TrainerModule:
    def __init__(
        self, *, model: nn.Module, checkpoint_path: str, log_dir: str, lr=1e-3, seed=42
    ):
        super().__init__()
        self.checkpoint_path = checkpoint_path
        self.lr = lr
        self.seed = seed
        # Create empty model. Note: no parameters yet
        self.model = model
        # Prepare logging
        self.logger = SummaryWriter(log_dir=log_dir)
        # Create jitted training and eval functions
        self.create_functions()
        # Initialize model
        # self.init_model()

    def create_functions(self):
        # Training function
        def train_step(state, rng, batch):
            imgs, _ = batch
            loss_fn = lambda params: self.model.apply(
                {"params": params}, imgs, rng, testing=False
            )
            (loss, rng), grads = jax.value_and_grad(loss_fn, has_aux=True)(
                state.params
            )  # Get loss and gradients for loss
            state = state.apply_gradients(grads=grads)  # Optimizer update step
            return state, rng, loss

        self.train_step = jax.jit(train_step)
        # Eval function, which is separately jitted for validation and testing
        def eval_step(state, rng, batch, testing):
            return self.model.apply(
                {"params": state.params}, batch[0], rng, testing=testing
            )

        self.eval_step = jax.jit(eval_step, static_argnums=(3,))

    def init_model(self, train_data_loader: DataLoader):
        exmp_imgs = next(iter(train_data_loader))[0]
        # Initialize model
        self.rng = jax.random.PRNGKey(self.seed)
        self.rng, init_rng, flow_rng = jax.random.split(self.rng, 3)
        params = self.model.init(init_rng, exmp_imgs, flow_rng)["params"]
        # Initialize learning rate schedule and optimizer
        lr_schedule = optax.exponential_decay(
            init_value=self.lr,
            transition_steps=len(train_data_loader),
            decay_rate=0.99,
            end_value=0.01 * self.lr,
        )
        optimizer = optax.chain(
            optax.clip_by_global_norm(1.0),  # Clip gradients at 1
            optax.adam(lr_schedule),
        )
        # Initialize training state
        self.state = train_state.TrainState.create(
            apply_fn=self.model.apply, params=params, tx=optimizer
        )

    def train_model(
        self, train_loader: DataLoader, val_loader: DataLoader, num_epochs=500
    ):
        self.init_model(train_loader)
        # Train model for defined number of epochs
        best_eval = 1e6
        for epoch_idx in tqdm(range(1, num_epochs + 1)):
            self.train_epoch(train_loader, epoch=epoch_idx)
            if epoch_idx % 5 == 0:
                eval_bpd = self.eval_model(val_loader, testing=False)
                self.logger.add_scalar("val/bpd", eval_bpd, global_step=epoch_idx)
                if eval_bpd < best_eval:
                    best_eval = eval_bpd
                    self.save_model(step=epoch_idx)
                self.logger.flush()

    def train_epoch(self, data_loader: DataLoader, epoch: int):
        # Train model for one epoch, and log avg loss
        avg_loss = 0.0
        for batch in tqdm(data_loader, leave=False):
            self.state, self.rng, loss = self.train_step(self.state, self.rng, batch)
            avg_loss += loss
        avg_loss /= len(data_loader)
        self.logger.add_scalar("train/bpd", avg_loss.item(), global_step=epoch)

    def eval_model(self, data_loader: DataLoader, testing=False):
        # Test model on all images of a data loader and return avg loss
        losses = []
        batch_sizes = []
        for batch in data_loader:
            loss, self.rng = self.eval_step(
                self.state, self.rng, batch, testing=testing
            )
            losses.append(loss)
            batch_sizes.append(batch[0].shape[0])
        losses_np = np.stack(jax.device_get(losses))
        batch_sizes_np = np.stack(batch_sizes)
        avg_loss = (losses_np * batch_sizes_np).sum() / batch_sizes_np.sum()
        return avg_loss

    def save_model(self, step=0):
        # Save current model at certain training iteration
        checkpoints.save_checkpoint(
            ckpt_dir=self.checkpoint_path, target=self.state.params, step=step
        )

    def load_model(self, pretrained=False):
        # Load model. We use different checkpoint for pretrained models
        if not pretrained:
            params = checkpoints.restore_checkpoint(
                ckpt_dir=self.checkpoint_path, target=self.state.params
            )
        else:
            params = checkpoints.restore_checkpoint(
                ckpt_dir=self.checkpoint_path,
                target=self.state.params,
            )
        self.state = train_state.TrainState.create(
            apply_fn=self.model.apply, params=params, tx=self.state.tx
        )

    def checkpoint_exists(self):
        # Check whether a pretrained model exist for this autoencoder
        return os.path.isfile(self.checkpoint_path)

    @staticmethod
    def train_flow(
        *,
        model: nn.Module,
        checkpoint_path: str,
        log_dir: str,
        train_loader: DataLoader,
        val_loader: DataLoader,
    ):
        # Create a trainer module with specified hyperparameters
        trainer = TrainerModule(model, checkpoint_path, log_dir, seed)
        if not trainer.checkpoint_exists():  # Skip training if pretrained model exists
            trainer.train_model(train_data_loader, val_loader, num_epochs=200)
            trainer.load_model()
            val_bpd = trainer.eval_model(val_loader, testing=True)
            start_time = time.time()
            test_bpd = trainer.eval_model(test_loader, testing=True)
            duration = time.time() - start_time
            results = {
                "val": val_bpd,
                "test": test_bpd,
                "time": duration / len(test_loader) / trainer.model.import_samples,
            }
        else:
            trainer.load_model(pretrained=True)
            with open(
                os.path.join(CHECKPOINT_PATH, f"{trainer.model_name}_results.json"), "r"
            ) as f:
                results = json.load(f)

        # Bind parameters to model for easier inference
        trainer.model_bd = trainer.model.bind({"params": trainer.state.params})
        return trainer, results
