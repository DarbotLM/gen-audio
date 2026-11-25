using PowerTorch;
using PowerTorch.Core;

namespace WingenAudio.VibeVoice;

public sealed class Qwen2DecoderModel
{
    private readonly ITensorBackend _backend;
    private readonly Qwen2DecoderConfiguration _configuration;
    private readonly Tensor _embeddingWeight;
    private readonly Tensor _finalNormWeight;
    private readonly Qwen2DecoderLayer[] _layers;

    public Qwen2DecoderModel(
        ITensorBackend backend,
        Qwen2DecoderConfiguration configuration,
        IReadOnlyDictionary<string, Tensor> state)
    {
        _backend = backend;
        _configuration = configuration;
        _embeddingWeight = Require(state, "model.language_model.embed_tokens.weight");
        _finalNormWeight = Require(state, "model.language_model.norm.weight");
        _layers = Enumerable.Range(0, configuration.Layers)
            .Select(layer => new Qwen2DecoderLayer(
                backend,
                configuration,
                Qwen2TensorNames.ForLayer(layer),
                state))
            .ToArray();
    }

    public Tensor Forward(Tensor inputIds, Tensor positions)
    {
        Tensor hidden = _backend.Embedding(_embeddingWeight, inputIds);
        try
        {
            foreach (Qwen2DecoderLayer layer in _layers)
            {
                Tensor next = layer.Forward(hidden, positions);
                hidden.Dispose();
                hidden = next;
            }
            return _backend.RmsNorm(
                hidden,
                [_configuration.HiddenSize],
                _finalNormWeight,
                _configuration.RmsNormEpsilon);
        }
        finally
        {
            hidden.Dispose();
        }
    }

    private static Tensor Require(IReadOnlyDictionary<string, Tensor> state, string name) =>
        state.TryGetValue(name, out Tensor? tensor)
            ? tensor
            : throw new KeyNotFoundException($"Decoder state is missing '{name}'.");
}

public sealed record Qwen2TensorNames(
    string InputNorm,
    string QueryWeight,
    string? QueryBias,
    string KeyWeight,
    string? KeyBias,
    string ValueWeight,
    string? ValueBias,
    string OutputWeight,
    string GateWeight,
    string UpWeight,
    string DownWeight,
    string PostAttentionNorm)
{
    public static Qwen2TensorNames ForLayer(int layer)
    {
        string prefix = $"model.language_model.layers.{layer}.";
        return new(
            prefix + "input_layernorm.weight",
            prefix + "self_attn.q_proj.weight",
            prefix + "self_attn.q_proj.bias",
            prefix + "self_attn.k_proj.weight",
            prefix + "self_attn.k_proj.bias",
            prefix + "self_attn.v_proj.weight",
            prefix + "self_attn.v_proj.bias",
            prefix + "self_attn.o_proj.weight",
            prefix + "mlp.gate_proj.weight",
            prefix + "mlp.up_proj.weight",
            prefix + "mlp.down_proj.weight",
            prefix + "post_attention_layernorm.weight");
    }
}

public sealed class Qwen2DecoderLayer
{
    private readonly ITensorBackend _backend;
    private readonly Qwen2DecoderConfiguration _configuration;
    private readonly Tensor _inputNormWeight;
    private readonly Qwen2Attention _attention;
    private readonly Tensor _postAttentionNormWeight;
    private readonly Tensor _gateWeight;
    private readonly Tensor _upWeight;
    private readonly Tensor _downWeight;

    public Qwen2DecoderLayer(
        ITensorBackend backend,
        Qwen2DecoderConfiguration configuration,
        Qwen2TensorNames names,
        IReadOnlyDictionary<string, Tensor> state)
    {
        _backend = backend;
        _configuration = configuration;
        _inputNormWeight = Require(state, names.InputNorm);
        _attention = new Qwen2Attention(
            backend,
            configuration,
            Require(state, names.QueryWeight),
            Optional(state, names.QueryBias),
            Require(state, names.KeyWeight),
            Optional(state, names.KeyBias),
            Require(state, names.ValueWeight),
            Optional(state, names.ValueBias),
            Require(state, names.OutputWeight));
        _postAttentionNormWeight = Require(state, names.PostAttentionNorm);
        _gateWeight = Require(state, names.GateWeight);
        _upWeight = Require(state, names.UpWeight);
        _downWeight = Require(state, names.DownWeight);
    }

    public Tensor Forward(Tensor input, Tensor positions)
    {
        using Tensor attentionInput = _backend.RmsNorm(
            input,
            [_configuration.HiddenSize],
            _inputNormWeight,
            _configuration.RmsNormEpsilon);
        using Tensor attentionOutput = _attention.Forward(attentionInput, positions);
        Tensor afterAttention = _backend.Add(input, attentionOutput);
        try
        {
            using Tensor mlpInput = _backend.RmsNorm(
                afterAttention,
                [_configuration.HiddenSize],
                _postAttentionNormWeight,
                _configuration.RmsNormEpsilon);
            using Tensor gated = TensorLinear.Forward(_backend, mlpInput, _gateWeight);
            using Tensor activated = TensorMath.SiLu(gated);
            using Tensor up = TensorLinear.Forward(_backend, mlpInput, _upWeight);
            using Tensor product = _backend.Multiply(activated, up);
            using Tensor mlpOutput = TensorLinear.Forward(_backend, product, _downWeight);
            return _backend.Add(afterAttention, mlpOutput);
        }
        finally
        {
            afterAttention.Dispose();
        }
    }

    private static Tensor Require(IReadOnlyDictionary<string, Tensor> state, string name) =>
        state.TryGetValue(name, out Tensor? tensor)
            ? tensor
            : throw new KeyNotFoundException($"Decoder state is missing '{name}'.");

    private static Tensor? Optional(
        IReadOnlyDictionary<string, Tensor> state,
        string? name) =>
        name is not null && state.TryGetValue(name, out Tensor? tensor) ? tensor : null;
}

public sealed class Qwen2Attention
{
    private readonly ITensorBackend _backend;
    private readonly Qwen2DecoderConfiguration _configuration;
    private readonly Tensor _queryWeight;
    private readonly Tensor? _queryBias;
    private readonly Tensor _keyWeight;
    private readonly Tensor? _keyBias;
    private readonly Tensor _valueWeight;
    private readonly Tensor? _valueBias;
    private readonly Tensor _outputWeight;

    internal Qwen2Attention(
        ITensorBackend backend,
        Qwen2DecoderConfiguration configuration,
        Tensor queryWeight,
        Tensor? queryBias,
        Tensor keyWeight,
        Tensor? keyBias,
        Tensor valueWeight,
        Tensor? valueBias,
        Tensor outputWeight)
    {
        _backend = backend;
        _configuration = configuration;
        _queryWeight = queryWeight;
        _queryBias = queryBias;
        _keyWeight = keyWeight;
        _keyBias = keyBias;
        _valueWeight = valueWeight;
        _valueBias = valueBias;
        _outputWeight = outputWeight;
    }

    public Tensor Forward(Tensor input, Tensor positions)
    {
        int headWidth = _configuration.HiddenSize / _configuration.AttentionHeads;
        long batch = input.Shape[0];
        long sequence = input.Shape[1];
        using Tensor queryProjection = TensorLinear.Forward(
            _backend, input, _queryWeight, _queryBias);
        using Tensor keyProjection = TensorLinear.Forward(
            _backend, input, _keyWeight, _keyBias);
        using Tensor valueProjection = TensorLinear.Forward(
            _backend, input, _valueWeight, _valueBias);
        using Tensor queryShaped = _backend.Reshape(
            queryProjection,
            [batch, sequence, _configuration.AttentionHeads, headWidth]);
        using Tensor keyShaped = _backend.Reshape(
            keyProjection,
            [batch, sequence, _configuration.KeyValueHeads, headWidth]);
        using Tensor valueShaped = _backend.Reshape(
            valueProjection,
            [batch, sequence, _configuration.KeyValueHeads, headWidth]);
        using Tensor queryHeads = _backend.Permute(queryShaped, [0, 2, 1, 3]);
        using Tensor keyHeads = _backend.Permute(keyShaped, [0, 2, 1, 3]);
        using Tensor valueHeads = _backend.Permute(valueShaped, [0, 2, 1, 3]);
        using Tensor rotatedQuery = _backend.Rotary(
            queryHeads,
            positions,
            sequenceDimension: 2,
            rotaryDimension: headWidth,
            baseFrequency: _configuration.RopeTheta,
            interleaved: false);
        using Tensor rotatedKey = _backend.Rotary(
            keyHeads,
            positions,
            sequenceDimension: 2,
            rotaryDimension: headWidth,
            baseFrequency: _configuration.RopeTheta,
            interleaved: false);
        using Tensor attention = _backend.CausalAttention(
            rotatedQuery,
            rotatedKey,
            valueHeads,
            scale: null);
        using Tensor mergedHeads = _backend.Permute(attention, [0, 2, 1, 3]);
        using Tensor merged = _backend.Reshape(
            mergedHeads,
            [batch, sequence, _configuration.HiddenSize]);
        return TensorLinear.Forward(_backend, merged, _outputWeight);
    }
}

internal static class TensorLinear
{
    public static Tensor Forward(
        ITensorBackend backend,
        Tensor input,
        Tensor weight,
        Tensor? bias = null)
    {
        if (weight.Shape.Count != 2 || input.Shape[^1] != weight.Shape[1])
        {
            throw new ArgumentException("Linear input and weight shapes are incompatible.");
        }
        using Tensor transposed = backend.Transpose(weight, 0, 1);
        Tensor output = backend.Matmul(input, transposed);
        if (bias is null)
        {
            return output;
        }
        try
        {
            return AddBias(output, bias);
        }
        finally
        {
            output.Dispose();
        }
    }

    private static Tensor AddBias(Tensor input, Tensor bias)
    {
        if (bias.Shape.Count != 1 ||
            input.Shape[^1] != bias.Shape[0] ||
            input.DataType != bias.DataType ||
            input.Device != bias.Device)
        {
            throw new ArgumentException("Linear bias must be a matching one-dimensional tensor.");
        }
        Tensor output = input.Clone();
        long width = bias.Shape[0];
        if (input.DataType == DataType.BFloat16)
        {
            Span<BFloat16> target = output.AsSpan<BFloat16>();
            ReadOnlySpan<BFloat16> source = bias.AsSpan<BFloat16>();
            for (int index = 0; index < target.Length; index++)
            {
                target[index] =
                    (BFloat16)((float)target[index] + source[index % checked((int)width)]);
            }
        }
        else if (input.DataType == DataType.Float32)
        {
            Span<float> target = output.AsSpan<float>();
            ReadOnlySpan<float> source = bias.AsSpan<float>();
            for (int index = 0; index < target.Length; index++)
            {
                target[index] += source[index % checked((int)width)];
            }
        }
        else
        {
            output.Dispose();
            throw new NotSupportedException($"Linear bias is not implemented for {input.DataType}.");
        }
        return output;
    }
}

internal static class TensorMath
{
    public static Tensor SiLu(Tensor input)
    {
        if (input.Device.Kind != DeviceKind.Cpu)
        {
            throw new NotSupportedException("SiLU currently requires CPU-resident tensors.");
        }
        Tensor output = TensorFactory.Empty(input.DataType, input.Shape.ToArray());
        if (input.DataType == DataType.BFloat16)
        {
            Span<BFloat16> destination = output.AsSpan<BFloat16>();
            ReadOnlySpan<BFloat16> source = input.AsSpan<BFloat16>();
            for (int index = 0; index < source.Length; index++)
            {
                float value = source[index];
                destination[index] = (BFloat16)(value / (1.0f + MathF.Exp(-value)));
            }
        }
        else if (input.DataType == DataType.Float32)
        {
            Span<float> destination = output.AsSpan<float>();
            ReadOnlySpan<float> source = input.AsSpan<float>();
            for (int index = 0; index < source.Length; index++)
            {
                float value = source[index];
                destination[index] = value / (1.0f + MathF.Exp(-value));
            }
        }
        else
        {
            output.Dispose();
            throw new NotSupportedException($"SiLU is not implemented for {input.DataType}.");
        }
        return output;
    }
}
