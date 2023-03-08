from typing import Callable

import e3nn_jax as e3nn
import flax
import jax
import jax.numpy as jnp


class NEQUIPLayer(flax.linen.Module):
    avg_num_neighbors: float
    num_species: int = 1
    sh_lmax: int = 3
    num_features: int = 64
    hidden_irreps: e3nn.Irreps = e3nn.Irreps("0e + 1o + 2e")
    even_activation: Callable[[jnp.ndarray], jnp.ndarray] = jax.nn.swish
    odd_activation: Callable[[jnp.ndarray], jnp.ndarray] = jax.nn.tanh
    mlp_activation: Callable[[jnp.ndarray], jnp.ndarray] = jax.nn.swish
    mlp_n_hidden: int = 64
    mlp_n_layers: int = 2

    @flax.linen.compact
    def __call__(
        self,
        vectors: e3nn.IrrepsArray,  # [n_edges, 3]
        node_feats: e3nn.IrrepsArray,  # [n_nodes, irreps]
        node_specie: jnp.ndarray,  # [n_nodes] int between 0 and num_species-1
        senders: jnp.ndarray,  # [n_edges]
        receivers: jnp.ndarray,  # [n_edges]
    ):
        n_edge = vectors.shape[0]
        n_node = node_feats.shape[0]
        assert vectors.shape == (n_edge, 3)
        assert node_feats.shape == (n_node, node_feats.irreps.dim)
        assert node_specie.shape == (n_node,)
        assert senders.shape == (n_edge,)
        assert receivers.shape == (n_edge,)

        lengths = e3nn.norm(vectors)  # [n_edges, (0e).dim]

        basis = e3nn.bessel(lengths.array[:, 0], 8)  # [n_edges, num_basis]
        cutoff = e3nn.poly_envelope(5, 2)(lengths.array)  # [n_edges, 1]
        radial_embedding = basis * cutoff  # [n_edges, num_basis]

        edge_attrs = e3nn.concatenate(
            [
                radial_embedding,
                e3nn.spherical_harmonics(
                    [l for l in range(1, self.sh_lmax + 1)],
                    vectors / lengths,
                    normalize=False,
                    normalization="component",
                ),
            ]
        )  # [n_edges, irreps]

        target_irreps = self.num_features * e3nn.Irreps(self.hidden_irreps)

        # TODO: check that it's equivariant to what is done in NEQUIP
        sc = e3nn.flax.Linear(
            target_irreps, num_indexed_weights=self.num_species, name="skip_tp"
        )(
            node_specie, node_feats
        )  # [n_nodes, feature * hidden_irreps]

        node_feats = e3nn.flax.Linear(node_feats.irreps, name="linear_up")(node_feats)

        # Add extra scalars for the gate activation
        target_irreps += target_irreps.filter(drop="0e").num_irreps * e3nn.Irreps("0e")

        node_feats = MessagePassingConvolution(
            self.avg_num_neighbors,
            target_irreps,
            self.mlp_activation,
            self.mlp_n_hidden,
            self.mlp_n_layers,
        )(node_feats, edge_attrs, senders, receivers)

        node_feats = e3nn.flax.Linear(target_irreps, name="linear_down")(node_feats)

        node_feats = e3nn.gate(
            node_feats,
            even_act=self.even_activation,
            even_gate_act=self.even_activation,
            odd_act=self.odd_activation,
            odd_gate_act=self.odd_activation,
        )

        node_feats = node_feats + sc  # [n_nodes, feature * hidden_irreps]

        return node_feats


class MessagePassingConvolution(flax.linen.Module):
    avg_num_neighbors: float
    target_irreps: e3nn.Irreps
    activation: Callable[[jnp.ndarray], jnp.ndarray] = jax.nn.swish
    mlp_n_hidden: int = 64
    mlp_n_layers: int = 2

    @flax.linen.compact
    def __call__(
        self,
        node_feats: e3nn.IrrepsArray,  # [n_nodes, irreps]
        edge_attrs: e3nn.IrrepsArray,  # [n_edges, irreps]
        senders: jnp.ndarray,  # [n_edges, ]
        receivers: jnp.ndarray,  # [n_edges, ]
    ) -> e3nn.IrrepsArray:
        assert node_feats.ndim == 2
        assert edge_attrs.ndim == 2

        messages = node_feats[senders]

        messages = e3nn.concatenate(
            [
                messages.filter(self.target_irreps),
                e3nn.tensor_product(
                    messages,
                    edge_attrs.filter(drop="0e"),
                    filter_ir_out=self.target_irreps,
                ),
            ]
        ).regroup()  # [n_edges, irreps]

        mix = e3nn.flax.MultiLayerPerceptron(
            self.mlp_n_layers * (self.mlp_n_hidden,) + (messages.irreps.num_irreps,),
            self.activation,
            output_activation=False,
        )(
            edge_attrs.filter(keep="0e")
        )  # [n_edges, num_irreps]

        messages = messages * mix  # [n_edges, irreps]

        zeros = e3nn.IrrepsArray.zeros(
            messages.irreps, node_feats.shape[:1], messages.dtype
        )
        node_feats = zeros.at[receivers].add(messages)  # [n_nodes, irreps]

        return node_feats / jnp.sqrt(self.avg_num_neighbors)
