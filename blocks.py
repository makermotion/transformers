import math

import torch
import torch.nn as nn
import torch.nn.functional as F


class Embedding(nn.Module):
    def __init__(self, vocab_size, embed_dim):
        """
        Parameters:
            vocab_size: size of the vocabulary
            embed_dim: dimension of embeddings
        """
        super().__init__()
        self.embed = nn.Embedding(vocab_size, embed_dim)

    def forward(self, x):
        """
        Parameters:
            x: input vector
        Returns:
            embedding vector
        """
        return self.embed(x)


class RotaryPositionalEncoding(nn.Module):
    def __init__(self, dim, max_seq_len):
        """
        Rotary Position Embedding (RoPE) implementation
        Parameters:
            dim: dimension of embeddings (must be divisible by 2)
            max_seq_len: maximum sequence length
        """
        super().__init__()
        self.dim = dim
        self.max_seq_len = max_seq_len

        # Generate inverse frequency bands for half the dimension (since we apply to pairs)
        inv_freq = 1.0 / (10000 ** (torch.arange(0, dim, 2).float() / dim))
        self.register_buffer("inv_freq", inv_freq)

        # Cache position embeddings
        self._update_cos_sin_cache(max_seq_len)

    def _update_cos_sin_cache(self, seq_len):
        """Update cached position embeddings"""
        positions = torch.arange(seq_len).float()
        freqs = torch.einsum("i,j->ij", positions, self.inv_freq)
        # [seq_len, dim/2]

        self.register_buffer("cos_cached", torch.cos(freqs))
        self.register_buffer("sin_cached", torch.sin(freqs))

    def forward(self, x):
        """
        Apply rotary position embeddings to input tensor
        Args:
            x: Input tensor of shape [batch_size, seq_len, dim] or [batch_size, n_heads, seq_len, head_dim]
        """
        if len(x.shape) == 4:
            batch_size, n_heads, seq_len, head_dim = x.shape
            reshape_shape = (batch_size, n_heads, seq_len, head_dim // 2, 2)
        else:
            batch_size, seq_len, dim = x.shape
            reshape_shape = (batch_size, seq_len, dim // 2, 2)

        if seq_len > self.max_seq_len:
            self._update_cos_sin_cache(seq_len)
            self.max_seq_len = seq_len

        cos = self.cos_cached[:seq_len, :]  # [seq_len, dim/2]
        sin = self.sin_cached[:seq_len, :]  # [seq_len, dim/2]

        # Reshape x to separate out last dimension pairs
        x_2 = x.view(*reshape_shape)
        x_2_complex = torch.view_as_complex(x_2)

        # Get rotation vectors
        if len(x.shape) == 4:
            cos = cos.unsqueeze(0).unsqueeze(0)  # [1, 1, seq_len, dim/2]
            sin = sin.unsqueeze(0).unsqueeze(0)  # [1, 1, seq_len, dim/2]
        else:
            cos = cos.unsqueeze(0)  # [1, seq_len, dim/2]
            sin = sin.unsqueeze(0)  # [1, seq_len, dim/2]

        rotation = torch.view_as_complex(torch.stack([cos, sin], dim=-1))
        return torch.view_as_real(x_2_complex * rotation).flatten(-2)


class PositionalEncoding(nn.Module):
    def __init__(self, max_seq_len, embed_dim):
        """
        Class to calculate Positional Encoding
        Parameters:
            max_seq_len: maximum sequence length to expect
            emded_dim: dimension of word embedding
        """
        super().__init__()
        self.embed_dim = embed_dim

        # create a null matrix
        pos_enc = torch.zeros(max_seq_len, self.embed_dim)

        # apply the sine and cosine functions from 'attention is all you need' paper
        for pos in range(max_seq_len):
            for i in range(0, self.embed_dim, 2):
                pos_enc[pos, i] = math.sin(pos / 10000 ** ((2 * i) / self.embed_dim))
                pos_enc[pos, i + 1] = math.cos(pos / 10000 ** ((2 * (i + 1)) / self.embed_dim))

        pos_enc = pos_enc.unsqueeze(0)

        # register buffer in Pytorch ->
        # If you have parameters in your model, which should be saved and restored in the state_dict,
        # but not trained by the optimizer, you should register them as buffers.
        self.register_buffer("pos_enc", pos_enc)

    def forward(self, x):
        """
        Args:
            x: input vector
        Returns:
            x: output
        """

        x = x + self.pos_enc[:, : x.size(1), :]
        return x


class MultiHeadAttention(nn.Module):
    def __init__(self, embed_dim, n_heads, pos_encoding_type, max_seq_len=512, dropout_rate=0.2):
        """
        Enhanced Multi-Head Attention with flash attention support and other optimizations
        Parameters:
            embed_dim: dimension of embeddings
            n_heads: number of attention heads
            pos_encoding_type: type of positional encoding ("sinusoidal" or "rotary")
            max_seq_len: maximum sequence length
            dropout_rate: dropout rate
        """
        super().__init__()
        self.embed_dim = embed_dim
        self.n_heads = n_heads
        self.head_dim = embed_dim // n_heads
        self.dropout_rate = dropout_rate
        self.pos_encoding_type = pos_encoding_type
        self.scale = self.head_dim**-0.5

        if pos_encoding_type == "rotary":
            self.rope = RotaryPositionalEncoding(self.head_dim, max_seq_len)

        assert self.head_dim * n_heads == embed_dim, "Embedding dimension must be divisible by number of heads"

        # Combine Q,K,V projections into a single linear layer for efficiency
        self.qkv = nn.Linear(embed_dim, 3 * embed_dim, bias=False)
        self.out_proj = nn.Linear(embed_dim, embed_dim, bias=False)

        # Initialize weights with small values for better training stability
        nn.init.normal_(self.qkv.weight, std=0.02)
        nn.init.normal_(self.out_proj.weight, std=0.02)

    def _flash_attention(self, q, k, v, mask=None):
        """
        Efficient attention implementation using flash attention if available
        """
        try:
            from flash_attn import flash_attn_func

            # Flash attention expects shape (batch, seqlen, nheads, headdim)
            q = q.transpose(1, 2)
            k = k.transpose(1, 2)
            v = v.transpose(1, 2)

            output = flash_attn_func(q, k, v, dropout_p=self.dropout_rate, causal=mask is not None)
            return output.transpose(1, 2)
        except ImportError:
            return self._standard_attention(q, k, v, mask)

    def _standard_attention(self, q, k, v, mask=None):
        """
        Standard scaled dot-product attention with optimizations
        """
        # Scaled dot-product attention
        attn = (q @ k.transpose(-2, -1)) * self.scale

        if mask is not None:
            attn = attn.masked_fill(mask == 0, float("-inf"))

        # Use softmax with better numerical stability
        attn = F.softmax(attn, dim=-1, dtype=torch.float32).to(q.dtype)

        # Apply dropout directly to attention weights
        if self.training and self.dropout_rate > 0:
            attn = F.dropout(attn, p=self.dropout_rate)

        return attn @ v

    def forward(self, query, key, value, mask=None):
        """
        Enhanced forward pass with optimized attention computation
        """
        batch_size = query.size(0)

        # Fused QKV projection
        qkv = self.qkv(query).chunk(3, dim=-1)
        q, k, v = map(lambda t: t.view(batch_size, -1, self.n_heads, self.head_dim).transpose(1, 2), qkv)

        # Apply RoPE if selected
        if self.pos_encoding_type == "rotary":
            q = self.rope(q)
            k = self.rope(k)

        # Use flash attention if available, otherwise fall back to standard attention
        if hasattr(torch.backends, "cuda") and torch.backends.cuda.is_built() and query.is_cuda:
            attn_output = self._flash_attention(q, k, v, mask)
        else:
            attn_output = self._standard_attention(q, k, v, mask)

        # Combine heads and project output
        output = attn_output.transpose(1, 2).contiguous().view(batch_size, -1, self.embed_dim)
        output = self.out_proj(output)

        # Apply output dropout only once at the end
        if self.training and self.dropout_rate > 0:
            output = F.dropout(output, p=self.dropout_rate)

        return output


class GPTDecoderBlock(nn.Module):
    def __init__(self, embed_dim, expansion_factor=4, n_heads=8, pos_encoding_type="rotary", dropout_rate=0.2):
        super().__init__()

        self.attention = MultiHeadAttention(
            embed_dim, n_heads, pos_encoding_type=pos_encoding_type, dropout_rate=dropout_rate
        )
        self.norm_0 = nn.LayerNorm(embed_dim)
        self.norm_1 = nn.LayerNorm(embed_dim)
        self.ffn = nn.Sequential(
            nn.Linear(embed_dim, expansion_factor * embed_dim),
            nn.ReLU(),
            nn.Linear(expansion_factor * embed_dim, embed_dim),
            nn.Dropout(dropout_rate),
        )

    def forward(self, x, mask):
        att = self.attention(x, x, x, mask)
        add_and_norm = self.norm_0(att + x)
        ffn_out = self.ffn(add_and_norm)
        out = self.norm_1(ffn_out + add_and_norm)
        return out


class GPTDecoder(nn.Module):
    def __init__(
        self,
        target_vocab_size,
        embed_dim,
        seq_len,
        num_layers=2,
        expansion_factor=4,
        n_heads=8,
        dropout_rate=0.2,
        pos_encoding_type="rotary",
    ):
        super().__init__()

        self.word_embedding = Embedding(target_vocab_size, embed_dim)
        self.embed_dropout = nn.Dropout(dropout_rate)
        self.pos_enc = PositionalEncoding(seq_len, embed_dim)
        self.layers = nn.ModuleList(
            [
                GPTDecoderBlock(embed_dim, expansion_factor, n_heads, pos_encoding_type, dropout_rate)
                for _ in range(num_layers)
            ]
        )
        self.fully_connected = nn.Linear(embed_dim, target_vocab_size)
        self.norm = nn.LayerNorm(embed_dim)

    def forward(self, x, mask):
        x = self.word_embedding(x)
        x = self.pos_enc(x)

        for layer in self.layers:
            x = layer(x, mask)

        x = self.norm(x)
        out = self.fully_connected(x)
        return out


class TransformerEncoderBlock(nn.Module):
    def __init__(self, embed_dim, expansion_factor=4, n_heads=8, pos_encoding_type="rotary", dropout_rate=0.2):
        """
        Parameters:
            embed_dim: dimension of the embedding vector
            expansion_factor: factor which determines the dimension of the linear layer
            n_heads: number of attention heads.
        """
        super().__init__()

        self.attention = MultiHeadAttention(
            embed_dim, n_heads, pos_encoding_type=pos_encoding_type, dropout_rate=dropout_rate
        )
        self.norm1 = nn.LayerNorm(embed_dim)
        self.norm2 = nn.LayerNorm(embed_dim)

        self.feed_forward = nn.Sequential(
            nn.Linear(embed_dim, expansion_factor * embed_dim),
            nn.GELU(),
            nn.Linear(expansion_factor * embed_dim, embed_dim),
        )

        self.dropout1 = nn.Dropout(dropout_rate)
        self.dropout2 = nn.Dropout(dropout_rate)

    def forward(self, key, query, value):
        attention = self.attention(key, query, value)  # 32x10x512

        norm1_out = self.dropout1(self.norm1(attention + query))  # 32x10x512
        ff_out = self.feed_forward(norm1_out)  # 32x10x512 -> #32x10x2048 -> 32x10x512

        norm2_out = self.dropout2(self.norm2(ff_out + norm1_out))  # 32x10x512

        return norm2_out


class TransformerEncoder(nn.Module):
    def __init__(
        self, seq_len, vocab_size, embed_dim, num_layers=2, expansion_factor=4, n_heads=8, pos_encoding_type="rotary"
    ):
        """
        Parameters:
            seq_len: length of the sequence
            vocab_size: size of the vocabulary to the data
            embed_dim: dimension of embedding
            num_layers: number of encoder layers
            expansion_factor: factor which determines the linear layers in feed forward step
            n_heads: number of heads for multi head attention
        Returns:
            out: output of the encoder.
        """
        super().__init__()

        self.embedding_layer = Embedding(vocab_size, embed_dim)
        self.pos_encoding = PositionalEncoding(seq_len, embed_dim)
        self.layers = nn.ModuleList(
            [
                TransformerEncoderBlock(embed_dim, expansion_factor, n_heads, pos_encoding_type)
                for i in range(num_layers)
            ]
        )

    def forward(self, x):
        embed_out = self.embedding_layer(x)
        out = self.pos_encoding(embed_out)

        for layer in self.layers:
            out = layer(out, out, out)

        return out


class TransformerDecoderBlock(nn.Module):
    def __init__(self, embed_dim, expansion_factor=4, n_heads=8, pos_encoding_type="rotary", dropout_rate=0.2):
        super().__init__()

        self.attention = MultiHeadAttention(
            embed_dim, n_heads, pos_encoding_type=pos_encoding_type, dropout_rate=dropout_rate
        )
        self.norm = nn.LayerNorm(embed_dim)
        self.do = nn.Dropout(dropout_rate)
        self.encoder_block = TransformerEncoderBlock(embed_dim, expansion_factor, n_heads)

    def forward(self, key, x, value, mask):
        att = self.attention(x, x, x, mask)
        query = self.do(self.norm(att + x))
        out = self.encoder_block(key, query, value)
        return out


class TransformerDecoder(nn.Module):
    def __init__(
        self,
        target_vocab_size,
        embed_dim,
        seq_len,
        num_layers=2,
        expansion_factor=4,
        n_heads=8,
        dropout_rate=0.2,
        pos_encoding_type="rotary",
    ):
        super().__init__()

        self.word_embedding = Embedding(target_vocab_size, embed_dim)
        self.pos_enc = PositionalEncoding(seq_len, embed_dim)

        self.layers = nn.ModuleList(
            [
                TransformerDecoderBlock(embed_dim, expansion_factor, n_heads, pos_encoding_type)
                for _ in range(num_layers)
            ]
        )

        self.fully_connected = nn.Linear(embed_dim, target_vocab_size)
        self.do = nn.Dropout(dropout_rate)

    def forward(self, x, enc_out, mask):
        x = self.word_embedding(x)
        x = self.pos_enc(x)
        x = self.do(x)

        for layer in self.layers:
            x = layer(enc_out, x, enc_out, mask)

        out = F.softmax(self.fully_connected(x))
        return out
