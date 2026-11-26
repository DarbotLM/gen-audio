using PowerTorch;
using PowerTorch.Core;

namespace WingenAudio.VibeVoice;

public sealed class VibeVoiceAcousticTokenizer : IAcousticTokenizer
{
    private readonly VibeVoiceTokenizerEncoder _encoder;
    private readonly VibeVoiceTokenizerDecoder _decoder;

    public VibeVoiceAcousticTokenizer(
        ITensorBackend backend,
        AcousticTokenizerConfiguration configuration,
        IReadOnlyDictionary<string, Tensor> state)
    {
        _encoder = new(
            backend,
            configuration,
            state,
            "model.acoustic_tokenizer.encoder.");
        _decoder = new(
            backend,
            configuration,
            state,
            "model.acoustic_tokenizer.decoder.");
    }

    public int LatentDimension => _encoder.LatentDimension;

    public bool CanEncode => true;

    public bool CanDecode => true;

    public Tensor Encode(Tensor waveform) => _encoder.Forward(waveform);

    public Tensor Decode(Tensor latent) => _decoder.Forward(latent);

    public static async ValueTask<OwnedVibeVoiceAcousticTokenizer> LoadAsync(
        ITensorBackend backend,
        VibeVoiceStateMap stateMap,
        CancellationToken cancellationToken = default)
    {
        Dictionary<string, Tensor> state = await stateMap.LoadPrefixAsync(
            "model.acoustic_tokenizer.",
            cancellationToken);
        try
        {
            return new(
                new(
                    backend,
                    stateMap.Configuration.AcousticTokenizer,
                    state),
                state);
        }
        catch
        {
            foreach (Tensor tensor in state.Values)
            {
                tensor.Dispose();
            }
            throw;
        }
    }
}

public sealed class VibeVoiceSemanticTokenizer : ISemanticTokenizer
{
    private readonly VibeVoiceTokenizerEncoder _encoder;

    public VibeVoiceSemanticTokenizer(
        ITensorBackend backend,
        AcousticTokenizerConfiguration configuration,
        IReadOnlyDictionary<string, Tensor> state)
    {
        _encoder = new(
            backend,
            configuration,
            state,
            "model.semantic_tokenizer.encoder.");
    }

    public int LatentDimension => _encoder.LatentDimension;

    public bool CanEncode => true;

    public Tensor Encode(Tensor waveform) => _encoder.Forward(waveform);

    public static async ValueTask<OwnedVibeVoiceSemanticTokenizer> LoadAsync(
        ITensorBackend backend,
        VibeVoiceStateMap stateMap,
        CancellationToken cancellationToken = default)
    {
        Dictionary<string, Tensor> state = await stateMap.LoadPrefixAsync(
            "model.semantic_tokenizer.",
            cancellationToken);
        try
        {
            return new(
                new(
                    backend,
                    stateMap.Configuration.SemanticTokenizer,
                    state),
                state);
        }
        catch
        {
            foreach (Tensor tensor in state.Values)
            {
                tensor.Dispose();
            }
            throw;
        }
    }
}

public sealed class OwnedVibeVoiceAcousticTokenizer : IDisposable
{
    private readonly Dictionary<string, Tensor> _state;
    private bool _disposed;

    internal OwnedVibeVoiceAcousticTokenizer(
        VibeVoiceAcousticTokenizer tokenizer,
        Dictionary<string, Tensor> state)
    {
        Tokenizer = tokenizer;
        _state = state;
    }

    public VibeVoiceAcousticTokenizer Tokenizer { get; }

    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }
        _disposed = true;
        foreach (Tensor tensor in _state.Values)
        {
            tensor.Dispose();
        }
    }
}

public sealed class OwnedVibeVoiceSemanticTokenizer : IDisposable
{
    private readonly Dictionary<string, Tensor> _state;
    private bool _disposed;

    internal OwnedVibeVoiceSemanticTokenizer(
        VibeVoiceSemanticTokenizer tokenizer,
        Dictionary<string, Tensor> state)
    {
        Tokenizer = tokenizer;
        _state = state;
    }

    public VibeVoiceSemanticTokenizer Tokenizer { get; }

    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }
        _disposed = true;
        foreach (Tensor tensor in _state.Values)
        {
            tensor.Dispose();
        }
    }
}

internal sealed class VibeVoiceTokenizerEncoder
{
    private readonly ITensorBackend _backend;
    private readonly AcousticTokenizerConfiguration _configuration;
    private readonly Convolution[] _downsample;
    private readonly TokenizerBlock[][] _stages;
    private readonly Convolution _head;

    public VibeVoiceTokenizerEncoder(
        ITensorBackend backend,
        AcousticTokenizerConfiguration configuration,
        IReadOnlyDictionary<string, Tensor> state,
        string prefix)
    {
        _backend = backend;
        _configuration = configuration;
        int[] ratios = configuration.EncoderRatios.Reverse().ToArray();
        _downsample = new Convolution[configuration.EncoderDepths.Length];
        _downsample[0] = Convolution.Load(
            backend,
            state,
            prefix + "downsample_layers.0.0.conv.conv.weight",
            prefix + "downsample_layers.0.0.conv.conv.bias",
            stride: 1,
            groups: 1);
        for (int index = 1; index < _downsample.Length; index++)
        {
            _downsample[index] = Convolution.Load(
                backend,
                state,
                prefix + $"downsample_layers.{index}.0.conv.conv.weight",
                prefix + $"downsample_layers.{index}.0.conv.conv.bias",
                stride: ratios[index - 1],
                groups: 1);
        }
        _stages = LoadStages(
            backend,
            configuration,
            state,
            prefix,
            configuration.EncoderDepths,
            stage => configuration.Filters * (1 << stage));
        _head = Convolution.Load(
            backend,
            state,
            prefix + "head.conv.conv.weight",
            prefix + "head.conv.conv.bias",
            stride: 1,
            groups: 1);
    }

    public int LatentDimension => _configuration.VaeDimension;

    public Tensor Forward(Tensor waveform)
    {
        if (waveform.Shape.Count != 3 ||
            waveform.Shape[1] != _configuration.Channels)
        {
            throw new ArgumentException(
                "Tokenizer waveform must have [batch, channels, samples] shape.");
        }
        Tensor value = waveform;
        bool owns = false;
        try
        {
            for (int stage = 0; stage < _stages.Length; stage++)
            {
                Tensor downsampled = _downsample[stage].Forward(value);
                if (owns)
                {
                    value.Dispose();
                }
                value = downsampled;
                owns = true;
                foreach (TokenizerBlock block in _stages[stage])
                {
                    Tensor next = block.Forward(value);
                    value.Dispose();
                    value = next;
                }
            }
            using Tensor encoded = _head.Forward(value);
            return _backend.Permute(encoded, [0, 2, 1]);
        }
        finally
        {
            if (owns)
            {
                value.Dispose();
            }
        }
    }

    internal static TokenizerBlock[][] LoadStages(
        ITensorBackend backend,
        AcousticTokenizerConfiguration configuration,
        IReadOnlyDictionary<string, Tensor> state,
        string prefix,
        IReadOnlyList<int> depths,
        Func<int, int> channelAtStage)
    {
        var stages = new TokenizerBlock[depths.Count][];
        for (int stage = 0; stage < depths.Count; stage++)
        {
            stages[stage] = new TokenizerBlock[depths[stage]];
            for (int block = 0; block < depths[stage]; block++)
            {
                stages[stage][block] = new(
                    backend,
                    configuration,
                    state,
                    prefix + $"stages.{stage}.{block}.",
                    channelAtStage(stage));
            }
        }
        return stages;
    }
}

internal sealed class VibeVoiceTokenizerDecoder
{
    private readonly ITensorBackend _backend;
    private readonly Convolution _stem;
    private readonly TransposedConvolution[] _upsample;
    private readonly TokenizerBlock[][] _stages;
    private readonly Convolution _head;

    public VibeVoiceTokenizerDecoder(
        ITensorBackend backend,
        AcousticTokenizerConfiguration configuration,
        IReadOnlyDictionary<string, Tensor> state,
        string prefix)
    {
        _backend = backend;
        _stem = Convolution.Load(
            backend,
            state,
            prefix + "upsample_layers.0.0.conv.conv.weight",
            prefix + "upsample_layers.0.0.conv.conv.bias",
            stride: 1,
            groups: 1);
        _upsample = new TransposedConvolution[configuration.DecoderRatios.Length];
        for (int index = 0; index < _upsample.Length; index++)
        {
            _upsample[index] = TransposedConvolution.Load(
                backend,
                state,
                prefix + $"upsample_layers.{index + 1}.0.convtr.convtr.weight",
                prefix + $"upsample_layers.{index + 1}.0.convtr.convtr.bias",
                stride: configuration.DecoderRatios[index]);
        }
        int lastStage = configuration.DecoderDepths.Length - 1;
        _stages = VibeVoiceTokenizerEncoder.LoadStages(
            backend,
            configuration,
            state,
            prefix,
            configuration.DecoderDepths,
            stage => configuration.DecoderFilters * (1 << (lastStage - stage)));
        _head = Convolution.Load(
            backend,
            state,
            prefix + "head.conv.conv.weight",
            prefix + "head.conv.conv.bias",
            stride: 1,
            groups: 1);
    }

    public Tensor Forward(Tensor latent)
    {
        if (latent.Shape.Count != 3)
        {
            throw new ArgumentException("Tokenizer latent must have rank 3.");
        }
        Tensor channelFirst = _backend.Permute(latent, [0, 2, 1]);
        Tensor value = channelFirst;
        try
        {
            Tensor stem = _stem.Forward(value);
            value.Dispose();
            value = stem;
            foreach (TokenizerBlock block in _stages[0])
            {
                Tensor next = block.Forward(value);
                value.Dispose();
                value = next;
            }
            for (int index = 0; index < _upsample.Length; index++)
            {
                Tensor upsampled = _upsample[index].Forward(value);
                value.Dispose();
                value = upsampled;
                foreach (TokenizerBlock block in _stages[index + 1])
                {
                    Tensor next = block.Forward(value);
                    value.Dispose();
                    value = next;
                }
            }
            Tensor decoded = _head.Forward(value);
            return decoded;
        }
        finally
        {
            value.Dispose();
        }
    }
}

internal sealed class TokenizerBlock
{
    private readonly ITensorBackend _backend;
    private readonly AcousticTokenizerConfiguration _configuration;
    private readonly Tensor _norm;
    private readonly Tensor _gamma;
    private readonly Tensor _ffnNorm;
    private readonly Tensor _ffnGamma;
    private readonly Tensor _linear1Weight;
    private readonly Tensor _linear1Bias;
    private readonly Tensor _linear2Weight;
    private readonly Tensor _linear2Bias;
    private readonly Convolution _mixer;
    private readonly int _channels;

    public TokenizerBlock(
        ITensorBackend backend,
        AcousticTokenizerConfiguration configuration,
        IReadOnlyDictionary<string, Tensor> state,
        string prefix,
        int channels)
    {
        _backend = backend;
        _configuration = configuration;
        _channels = channels;
        _norm = Require(state, prefix + "norm.weight");
        _gamma = Require(state, prefix + "gamma");
        _ffnNorm = Require(state, prefix + "ffn_norm.weight");
        _ffnGamma = Require(state, prefix + "ffn_gamma");
        _linear1Weight = Require(state, prefix + "ffn.linear1.weight");
        _linear1Bias = Require(state, prefix + "ffn.linear1.bias");
        _linear2Weight = Require(state, prefix + "ffn.linear2.weight");
        _linear2Bias = Require(state, prefix + "ffn.linear2.bias");
        _mixer = Convolution.Load(
            backend,
            state,
            prefix + "mixer.conv.conv.conv.weight",
            prefix + "mixer.conv.conv.conv.bias",
            stride: 1,
            groups: channels);
    }

    public Tensor Forward(Tensor input)
    {
        using Tensor normalized = ConvRmsNorm(input, _norm);
        using Tensor mixed = _mixer.Forward(normalized);
        using Tensor scaled = TensorBroadcast.MultiplyChannels(mixed, _gamma);
        Tensor residual = _backend.Add(input, scaled);
        try
        {
            using Tensor normalizedFfn = ConvRmsNorm(residual, _ffnNorm);
            using Tensor timeMajor = _backend.Transpose(normalizedFfn, 1, 2);
            using Tensor linear1 = TensorLinear.Forward(
                _backend,
                timeMajor,
                _linear1Weight,
                _linear1Bias);
            using Tensor activated = _backend.Gelu(linear1);
            using Tensor linear2 = TensorLinear.Forward(
                _backend,
                activated,
                _linear2Weight,
                _linear2Bias);
            using Tensor ffn = _backend.Transpose(linear2, 1, 2);
            using Tensor scaledFfn = TensorBroadcast.MultiplyChannels(ffn, _ffnGamma);
            return _backend.Add(residual, scaledFfn);
        }
        finally
        {
            residual.Dispose();
        }
    }

    private Tensor ConvRmsNorm(Tensor input, Tensor weight)
    {
        using Tensor timeMajor = _backend.Transpose(input, 1, 2);
        using Tensor normalized = _backend.RmsNorm(
            timeMajor,
            [_channels],
            weight,
            _configuration.NormEpsilon);
        return _backend.Transpose(normalized, 1, 2);
    }

    private static Tensor Require(IReadOnlyDictionary<string, Tensor> state, string name) =>
        state.TryGetValue(name, out Tensor? tensor)
            ? tensor
            : throw new KeyNotFoundException($"Tokenizer state is missing '{name}'.");
}

internal sealed class Convolution
{
    private readonly ITensorBackend _backend;
    private readonly Tensor _weight;
    private readonly Tensor? _bias;
    private readonly int _stride;
    private readonly int _groups;

    private Convolution(
        ITensorBackend backend,
        Tensor weight,
        Tensor? bias,
        int stride,
        int groups)
    {
        _backend = backend;
        _weight = weight;
        _bias = bias;
        _stride = stride;
        _groups = groups;
    }

    public static Convolution Load(
        ITensorBackend backend,
        IReadOnlyDictionary<string, Tensor> state,
        string weightName,
        string biasName,
        int stride,
        int groups)
    {
        Tensor weight = state.TryGetValue(weightName, out Tensor? foundWeight)
            ? foundWeight
            : throw new KeyNotFoundException($"Tokenizer state is missing '{weightName}'.");
        state.TryGetValue(biasName, out Tensor? bias);
        return new(backend, weight, bias, stride, groups);
    }

    public Tensor Forward(Tensor input) =>
        _backend.Conv1D(
            input,
            _weight,
            _bias,
            _stride,
            padding: 0,
            dilation: 1,
            _groups,
            causal: true);
}

internal sealed class TransposedConvolution
{
    private readonly ITensorBackend _backend;
    private readonly Tensor _weight;
    private readonly Tensor? _bias;
    private readonly int _stride;

    private TransposedConvolution(
        ITensorBackend backend,
        Tensor weight,
        Tensor? bias,
        int stride)
    {
        _backend = backend;
        _weight = weight;
        _bias = bias;
        _stride = stride;
    }

    public static TransposedConvolution Load(
        ITensorBackend backend,
        IReadOnlyDictionary<string, Tensor> state,
        string weightName,
        string biasName,
        int stride)
    {
        Tensor weight = state.TryGetValue(weightName, out Tensor? foundWeight)
            ? foundWeight
            : throw new KeyNotFoundException($"Tokenizer state is missing '{weightName}'.");
        state.TryGetValue(biasName, out Tensor? bias);
        return new(backend, weight, bias, stride);
    }

    public Tensor Forward(Tensor input)
    {
        using Tensor expanded = _backend.ConvTranspose1D(
            input,
            _weight,
            _bias,
            stride: _stride,
            padding: 0,
            outputPadding: 0,
            dilation: 1,
            groups: 1);
        long length = checked(input.Shape[2] * _stride);
        return _backend.Slice(expanded, 2, 0, length, 1);
    }
}

internal static class TensorBroadcast
{
    public static Tensor MultiplyChannels(Tensor input, Tensor channelWeights)
    {
        if (input.Shape.Count != 3 ||
            channelWeights.Shape.Count != 1 ||
            input.Shape[1] != channelWeights.Shape[0] ||
            input.DataType != channelWeights.DataType)
        {
            throw new ArgumentException("Channel weights must match [batch, channels, time] input.");
        }
        Tensor output = input.Clone();
        if (input.DataType == DataType.BFloat16)
        {
            Span<BFloat16> values = output.AsSpan<BFloat16>();
            ReadOnlySpan<BFloat16> weights = channelWeights.AsSpan<BFloat16>();
            long time = input.Shape[2];
            for (int index = 0; index < values.Length; index++)
            {
                int channel = checked((int)((index / time) % input.Shape[1]));
                values[index] = (BFloat16)((float)values[index] * weights[channel]);
            }
        }
        else if (input.DataType == DataType.Float32)
        {
            Span<float> values = output.AsSpan<float>();
            ReadOnlySpan<float> weights = channelWeights.AsSpan<float>();
            long time = input.Shape[2];
            for (int index = 0; index < values.Length; index++)
            {
                int channel = checked((int)((index / time) % input.Shape[1]));
                values[index] *= weights[channel];
            }
        }
        else
        {
            output.Dispose();
            throw new NotSupportedException(
                $"Channel multiplication is not implemented for {input.DataType}.");
        }
        return output;
    }
}
