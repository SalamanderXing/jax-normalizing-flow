from flax import linen as nn
import jax.numpy as jnp
import numpy as np


class CouplingLayer(nn.Module):
    network: nn.Module  # NN to use in the flow for predicting mu and sigma
    mask: np.ndarray  # Binary mask where 0 denotes that the element should be transformed, and 1 not.
    c_in: int  # Number of input channels

    def setup(self):
        self.scaling_factor = self.param(
            "scaling_factor", nn.initializers.zeros, (self.c_in,)
        )

    def __call__(self, z, ldj, rng, reverse=False, orig_img=None):
        """
        Inputs:
            z - Latent input to the flow
            ldj - The current ldj of the previous flows.
                  The ldj of this layer will be added to this tensor.
            rng - PRNG state
            reverse - If True, we apply the inverse of the layer.
            orig_img (optional) - Only needed in VarDeq. Allows external
                                  input to condition the flow on (e.g. original image)
        """
        # Apply network to masked input
        z_in = z * self.mask
        if orig_img is None:
            nn_out = self.network(z_in)
        else:
            nn_out = self.network(jnp.concatenate([z_in, orig_img], axis=-1))
        s, t = nn_out.split(2, axis=-1)

        # Stabilize scaling output
        s_fac = jnp.exp(self.scaling_factor).reshape(1, 1, 1, -1)
        s = nn.tanh(s / s_fac) * s_fac

        # Mask outputs (only transform the second part)
        s = s * (1 - self.mask)
        t = t * (1 - self.mask)

        # Affine transformation
        if not reverse:
            # Whether we first shift and then scale, or the other way round,
            # is a design choice, and usually does not have a big impact
            z = (z + t) * jnp.exp(s)
            ldj += s.sum(axis=(1, 2, 3))
        else:
            z = (z * jnp.exp(-s)) - t
            ldj -= s.sum(axis=(1, 2, 3))

        return z, ldj, rng
