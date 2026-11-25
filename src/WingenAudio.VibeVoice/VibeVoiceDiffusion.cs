using PowerTorch;
using PowerTorch.Core;

namespace WingenAudio.VibeVoice;

public sealed class VibeVoiceDiffusionHead : IDiffusionHead
{
    private readonly ITensorBackend _backend;
    private readonly DiffusionHeadConfiguration _configuration;
    private readonly Tensor _noisyProjection;
    private readonly Tensor _conditionProjection;
    private readonly Tensor _timeProjection0;
    private readonly Tensor _timeProjection2;
    private readonly DiffusionHeadLayer[] _layers;
    private readonly Tensor _finalModulation;
    private readonly Tensor _finalProjection;

    public VibeVoiceDiffusionHead(
        ITensorBackend backend,
        DiffusionHeadConfiguration configuration,
        IReadOnlyDictionary<string, Tensor> state)
    {
        _backend = backend;
        _configuration = configuration;
        _noisyProjection = Require(
            state,
            "model.prediction_head.noisy_images_proj.weight");
        _conditionProjection = Require(
            state,
            "model.prediction_head.cond_proj.weight");
        _timeProjection0 = Require(
            state,
            "model.prediction_head.t_embedder.mlp.0.weight");
        _timeProjection2 = Require(
            state,
            "model.prediction_head.t_embedder.mlp.2.weight");
        _layers = Enumerable.Range(0, configuration.Layers)
            .Select(layer => new DiffusionHeadLayer(
                backend,
                configuration,
                state,
                layer))
            .ToArray();
        _finalModulation = Require(
            state,
            "model.prediction_head.final_layer.adaLN_modulation.1.weight");
        _finalProjection = Require(
            state,
            "model.prediction_head.final_layer.linear.weight");
    }

    public int LatentDimension => _configuration.LatentSize;

    public int InferenceSteps => _configuration.InferenceSteps;

    public Tensor Predict(
        Tensor noisyLatent,
        Tensor timestep,
        Tensor condition)
    {
        if (noisyLatent.Shape.Count != 2 ||
            noisyLatent.Shape[1] != _configuration.LatentSize ||
            condition.Shape.Count != 2 ||
            condition.Shape[0] != noisyLatent.Shape[0] ||
            condition.Shape[1] != _configuration.HiddenSize ||
            timestep.Shape.Count != 1 ||
            timestep.Shape[0] != noisyLatent.Shape[0])
        {
            throw new ArgumentException(
                "Diffusion head expects [batch, latent], [batch], and [batch, hidden] inputs.");
        }
        using Tensor x0 = TensorLinear.Forward(
            _backend,
            noisyLatent,
            _noisyProjection);
        using Tensor condition0 = TensorLinear.Forward(
            _backend,
            condition,
            _conditionProjection);
        using Tensor time = TimestepEmbedding(timestep);
        using Tensor time0 = TensorLinear.Forward(
            _backend,
            time,
            _timeProjection0);
        using Tensor time1 = TensorMath.SiLu(time0);
        using Tensor time2 = TensorLinear.Forward(
            _backend,
            time1,
            _timeProjection2);
        Tensor x = x0.Clone();
        Tensor combined = _backend.Add(condition0, time2);
        try
        {
            foreach (DiffusionHeadLayer layer in _layers)
            {
                Tensor next = layer.Forward(x, combined);
                x.Dispose();
                x = next;
            }
            using Tensor normalized = _backend.RmsNorm(
                x,
                [_configuration.HiddenSize],
                weight: null,
                _configuration.RmsNormEpsilon);
            using Tensor activatedCondition = TensorMath.SiLu(combined);
            using Tensor modulation = TensorLinear.Forward(
                _backend,
                activatedCondition,
                _finalModulation);
            using Tensor shift = _backend.Slice(
                modulation,
                1,
                0,
                _configuration.HiddenSize,
                1);
            using Tensor scale = _backend.Slice(
                modulation,
                1,
                _configuration.HiddenSize,
                _configuration.HiddenSize * 2L,
                1);
            using Tensor modulated = TensorModulation.Apply(normalized, shift, scale);
            return TensorLinear.Forward(_backend, modulated, _finalProjection);
        }
        finally
        {
            x.Dispose();
            combined.Dispose();
        }
    }

    public Tensor PredictWithClassifierFreeGuidance(
        Tensor noisyLatent,
        Tensor timestep,
        Tensor conditional,
        Tensor unconditional,
        double scale)
    {
        if (!double.IsFinite(scale) || scale < 0 ||
            conditional.Shape[0] != unconditional.Shape[0] ||
            noisyLatent.Shape[0] != conditional.Shape[0])
        {
            throw new ArgumentException(
                "Classifier-free guidance requires matching conditions and finite non-negative scale.");
        }
        using Tensor combinedConditions = _backend.Concat(
            [conditional, unconditional],
            0);
        using Tensor combinedLatents = _backend.Concat(
            [noisyLatent, noisyLatent],
            0);
        using Tensor combinedTimesteps = _backend.Concat(
            [timestep, timestep],
            0);
        using Tensor predictions = Predict(
            combinedLatents,
            combinedTimesteps,
            combinedConditions);
        long batch = conditional.Shape[0];
        using Tensor positive = _backend.Slice(predictions, 0, 0, batch, 1);
        using Tensor negative = _backend.Slice(
            predictions,
            0,
            batch,
            batch * 2,
            1);
        return ApplyGuidance(negative, positive, scale);
    }

    public static Tensor ApplyGuidance(
        Tensor unconditional,
        Tensor conditional,
        double scale)
    {
        Tensor output = unconditional.Clone();
        ApplyBFloat16OrFloat32(
            output,
            conditional,
            (uncond, cond) => uncond + ((cond - uncond) * scale));
        return output;
    }

    public static async ValueTask<OwnedVibeVoiceDiffusionHead> LoadAsync(
        ITensorBackend backend,
        VibeVoiceStateMap stateMap,
        CancellationToken cancellationToken = default)
    {
        Dictionary<string, Tensor> state = await stateMap.LoadPrefixAsync(
            "model.prediction_head.",
            cancellationToken);
        try
        {
            return new(
                new(
                    backend,
                    stateMap.Configuration.DiffusionHead,
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

    private Tensor TimestepEmbedding(Tensor timestep)
    {
        if (timestep.DataType is not (DataType.Int32 or DataType.Int64))
        {
            throw new ArgumentException("Timestep tensor must contain integers.");
        }
        int batch = checked((int)timestep.Shape[0]);
        var values = new float[batch * 256];
        long[] timesteps = timestep.DataType == DataType.Int64
            ? timestep.AsSpan<long>().ToArray()
            : timestep.AsSpan<int>().ToArray().Select(value => (long)value).ToArray();
        const int half = 128;
        for (int row = 0; row < batch; row++)
        {
            if (timesteps[row] < 0)
            {
                throw new ArgumentOutOfRangeException(
                    nameof(timestep),
                    "Diffusion timesteps must be non-negative.");
            }
            for (int index = 0; index < half; index++)
            {
                double frequency =
                    Math.Exp((-Math.Log(10000.0) * index) / half);
                double angle = timesteps[row] * frequency;
                values[(row * 256) + index] = (float)Math.Cos(angle);
                values[(row * 256) + half + index] = (float)Math.Sin(angle);
            }
        }
        return TensorFactory.FromArray(
            values.Select(value => (BFloat16)value).ToArray(),
            [batch, 256]);
    }

    private static Tensor Require(
        IReadOnlyDictionary<string, Tensor> state,
        string name) =>
        state.TryGetValue(name, out Tensor? tensor)
            ? tensor
            : throw new KeyNotFoundException($"Diffusion state is missing '{name}'.");

    private static void ApplyBFloat16OrFloat32(
        Tensor destination,
        Tensor source,
        Func<double, double, double> operation)
    {
        if (destination.Shape.SequenceEqual(source.Shape) == false ||
            destination.DataType != source.DataType)
        {
            throw new ArgumentException("Guidance tensors must have matching shape and dtype.");
        }
        if (destination.DataType == DataType.BFloat16)
        {
            Span<BFloat16> left = destination.AsSpan<BFloat16>();
            ReadOnlySpan<BFloat16> right = source.AsSpan<BFloat16>();
            for (int index = 0; index < left.Length; index++)
            {
                left[index] = (BFloat16)operation(left[index], right[index]);
            }
            return;
        }
        if (destination.DataType == DataType.Float32)
        {
            Span<float> left = destination.AsSpan<float>();
            ReadOnlySpan<float> right = source.AsSpan<float>();
            for (int index = 0; index < left.Length; index++)
            {
                left[index] = (float)operation(left[index], right[index]);
            }
            return;
        }
        throw new NotSupportedException(
            $"Classifier-free guidance is not implemented for {destination.DataType}.");
    }
}

internal sealed class DiffusionHeadLayer
{
    private readonly ITensorBackend _backend;
    private readonly DiffusionHeadConfiguration _configuration;
    private readonly Tensor _norm;
    private readonly Tensor _modulation;
    private readonly Tensor _gate;
    private readonly Tensor _up;
    private readonly Tensor _down;

    public DiffusionHeadLayer(
        ITensorBackend backend,
        DiffusionHeadConfiguration configuration,
        IReadOnlyDictionary<string, Tensor> state,
        int layer)
    {
        _backend = backend;
        _configuration = configuration;
        string prefix = $"model.prediction_head.layers.{layer}.";
        _norm = Require(state, prefix + "norm.weight");
        _modulation = Require(state, prefix + "adaLN_modulation.1.weight");
        _gate = Require(state, prefix + "ffn.gate_proj.weight");
        _up = Require(state, prefix + "ffn.up_proj.weight");
        _down = Require(state, prefix + "ffn.down_proj.weight");
    }

    public Tensor Forward(Tensor input, Tensor condition)
    {
        int hidden = _configuration.HiddenSize;
        using Tensor normalized = _backend.RmsNorm(
            input,
            [hidden],
            _norm,
            _configuration.RmsNormEpsilon);
        using Tensor activatedCondition = TensorMath.SiLu(condition);
        using Tensor modulation = TensorLinear.Forward(
            _backend,
            activatedCondition,
            _modulation);
        using Tensor shift = _backend.Slice(modulation, 1, 0, hidden, 1);
        using Tensor scale = _backend.Slice(
            modulation,
            1,
            hidden,
            hidden * 2L,
            1);
        using Tensor gate = _backend.Slice(
            modulation,
            1,
            hidden * 2L,
            hidden * 3L,
            1);
        using Tensor modulated = TensorModulation.Apply(normalized, shift, scale);
        using Tensor gateProjection = TensorLinear.Forward(
            _backend,
            modulated,
            _gate);
        using Tensor activated = TensorMath.SiLu(gateProjection);
        using Tensor upProjection = TensorLinear.Forward(
            _backend,
            modulated,
            _up);
        using Tensor product = _backend.Multiply(activated, upProjection);
        using Tensor ffn = TensorLinear.Forward(_backend, product, _down);
        using Tensor gated = _backend.Multiply(gate, ffn);
        return _backend.Add(input, gated);
    }

    private static Tensor Require(
        IReadOnlyDictionary<string, Tensor> state,
        string name) =>
        state.TryGetValue(name, out Tensor? tensor)
            ? tensor
            : throw new KeyNotFoundException($"Diffusion state is missing '{name}'.");
}

public sealed class CosineDdpmScheduler
{
    private readonly ITensorBackend _backend;
    private readonly double[] _betas;
    private readonly double[] _alphaCumulative;

    public CosineDdpmScheduler(
        ITensorBackend backend,
        int trainingSteps,
        string predictionType,
        double maxBeta = 0.999)
    {
        ArgumentNullException.ThrowIfNull(backend);
        if (trainingSteps <= 1 ||
            maxBeta <= 0 ||
            maxBeta >= 1)
        {
            throw new ArgumentOutOfRangeException(
                nameof(trainingSteps),
                "Cosine DDPM requires at least two steps and 0 < maxBeta < 1.");
        }
        if (!string.Equals(
                predictionType,
                "v_prediction",
                StringComparison.OrdinalIgnoreCase))
        {
            throw new NotSupportedException(
                $"Cosine DDPM currently supports only v_prediction, not '{predictionType}'.");
        }
        _backend = backend;
        _betas = new double[trainingSteps];
        _alphaCumulative = new double[trainingSteps];
        double AlphaBar(double time)
        {
            double cosine = Math.Cos(
                ((time + 0.008) / 1.008) * Math.PI / 2.0);
            return cosine * cosine;
        }
        double cumulative = 1;
        for (int index = 0; index < trainingSteps; index++)
        {
            double beta = Math.Min(
                1.0 -
                    AlphaBar((index + 1.0) / trainingSteps) /
                    AlphaBar(index / (double)trainingSteps),
                maxBeta);
            _betas[index] = beta;
            cumulative *= 1.0 - beta;
            _alphaCumulative[index] = cumulative;
        }
    }

    public IReadOnlyList<double> Betas => _betas;

    public IReadOnlyList<double> AlphaCumulative => _alphaCumulative;

    public int[] SetTimesteps(int inferenceSteps)
    {
        if (inferenceSteps <= 0 || inferenceSteps > _betas.Length)
        {
            throw new ArgumentOutOfRangeException(
                nameof(inferenceSteps),
                "Inference steps must be within the training schedule.");
        }
        return Enumerable.Range(0, inferenceSteps + 1)
            .Select(index => checked((int)Math.Round(
                index * (_betas.Length - 1.0) / inferenceSteps)))
            .Reverse()
            .Take(inferenceSteps)
            .ToArray();
    }

    public Tensor AddNoise(
        Tensor clean,
        Tensor noise,
        Tensor timestep)
    {
        Tensor result = clean.Clone();
        int index = SingleTimestep(timestep);
        double alpha = _alphaCumulative[index];
        ApplyElementwise(
            result,
            noise,
            result,
            (noiseValue, cleanValue) =>
                (Math.Sqrt(alpha) * cleanValue) +
                (Math.Sqrt(1.0 - alpha) * noiseValue));
        return result;
    }

    public Tensor Step(
        Tensor velocity,
        Tensor timestep,
        Tensor sample,
        Tensor? noise = null,
        int? previousTimestep = null)
    {
        int current = SingleTimestep(timestep);
        int previous = previousTimestep ?? current - 1;
        if (previous < -1 || previous >= current)
        {
            throw new ArgumentOutOfRangeException(
                nameof(previousTimestep),
                "Previous timestep must be -1 or lower than the current timestep.");
        }
        double alpha = _alphaCumulative[current];
        double alphaPrevious = previous >= 0 ? _alphaCumulative[previous] : 1.0;
        double betaCurrent = 1.0 - (alpha / alphaPrevious);
        double coefficientSample =
            (Math.Sqrt(alphaPrevious) * betaCurrent) / (1.0 - alpha);
        double coefficientLatent =
            (Math.Sqrt(alpha) * (1.0 - alphaPrevious)) /
            (1.0 - alpha);
        double posteriorVariance =
            betaCurrent * (1.0 - alphaPrevious) / (1.0 - alpha);
        Tensor result = sample.Clone();
        Tensor? generatedNoise = noise ??
            (previous < 0
                ? null
                : _backend.RandomNormal(
                    sample.DataType,
                    sample.Shape,
                    0,
                    1,
                    seed: null));
        try
        {
            ApplyElementwise(
                result,
                velocity,
                sample,
                (velocityValue, sampleValue) =>
                {
                    double x0 =
                        (Math.Sqrt(alpha) * sampleValue) -
                        (Math.Sqrt(1.0 - alpha) * velocityValue);
                    return
                        (coefficientSample * x0) +
                        (coefficientLatent * sampleValue);
                });
            if (generatedNoise is not null)
            {
                AddScaledInPlace(
                    result,
                    generatedNoise,
                    Math.Sqrt(posteriorVariance));
            }
            return result;
        }
        catch
        {
            result.Dispose();
            throw;
        }
        finally
        {
            if (generatedNoise is not null &&
                !ReferenceEquals(generatedNoise, noise))
            {
                generatedNoise.Dispose();
            }
        }
    }

    private static void AddScaledInPlace(
        Tensor destination,
        Tensor source,
        double scale)
    {
        ApplyElementwise(
            destination,
            source,
            destination,
            (sourceValue, destinationValue) =>
                destinationValue + (scale * sourceValue));
    }

    private int SingleTimestep(Tensor timestep)
    {
        if (timestep.Shape.Count != 1 || timestep.ElementCount != 1)
        {
            throw new ArgumentException("Scheduler step currently requires one shared timestep.");
        }
        long value = timestep.DataType switch
        {
            DataType.Int64 => timestep.AsSpan<long>()[0],
            DataType.Int32 => timestep.AsSpan<int>()[0],
            _ => throw new ArgumentException("Scheduler timestep must be Int32 or Int64."),
        };
        if (value < 0 || value >= _betas.Length)
        {
            throw new ArgumentOutOfRangeException(
                nameof(timestep),
                "Scheduler timestep is outside the cosine schedule.");
        }
        return checked((int)value);
    }

    private static void ApplyElementwise(
        Tensor destination,
        Tensor right,
        Tensor sample,
        Func<double, double, double> operation)
    {
        if (destination.Shape.SequenceEqual(right.Shape) == false ||
            destination.Shape.SequenceEqual(sample.Shape) == false ||
            destination.DataType != right.DataType ||
            destination.DataType != sample.DataType)
        {
            throw new ArgumentException("Scheduler tensors must have matching shape and dtype.");
        }
        if (destination.DataType == DataType.BFloat16)
        {
            Span<BFloat16> output = destination.AsSpan<BFloat16>();
            ReadOnlySpan<BFloat16> rightValues = right.AsSpan<BFloat16>();
            ReadOnlySpan<BFloat16> sampleValues = sample.AsSpan<BFloat16>();
            for (int index = 0; index < output.Length; index++)
            {
                output[index] =
                    (BFloat16)operation(rightValues[index], sampleValues[index]);
            }
            return;
        }
        if (destination.DataType == DataType.Float32)
        {
            Span<float> output = destination.AsSpan<float>();
            ReadOnlySpan<float> rightValues = right.AsSpan<float>();
            ReadOnlySpan<float> sampleValues = sample.AsSpan<float>();
            for (int index = 0; index < output.Length; index++)
            {
                output[index] =
                    (float)operation(rightValues[index], sampleValues[index]);
            }
            return;
        }
        throw new NotSupportedException(
            $"Scheduler is not implemented for {destination.DataType}.");
    }
}

internal static class TensorModulation
{
    public static Tensor Apply(Tensor input, Tensor shift, Tensor scale)
    {
        Tensor output = input.Clone();
        if (!input.Shape.SequenceEqual(shift.Shape) ||
            !input.Shape.SequenceEqual(scale.Shape) ||
            input.DataType != shift.DataType ||
            input.DataType != scale.DataType)
        {
            throw new ArgumentException("AdaLN modulation tensors must match exactly.");
        }
        if (input.DataType == DataType.BFloat16)
        {
            Span<BFloat16> values = output.AsSpan<BFloat16>();
            ReadOnlySpan<BFloat16> shifts = shift.AsSpan<BFloat16>();
            ReadOnlySpan<BFloat16> scales = scale.AsSpan<BFloat16>();
            for (int index = 0; index < values.Length; index++)
            {
                values[index] =
                    (BFloat16)(((1.0 + scales[index]) * values[index]) + shifts[index]);
            }
            return output;
        }
        if (input.DataType == DataType.Float32)
        {
            Span<float> values = output.AsSpan<float>();
            ReadOnlySpan<float> shifts = shift.AsSpan<float>();
            ReadOnlySpan<float> scales = scale.AsSpan<float>();
            for (int index = 0; index < values.Length; index++)
            {
                values[index] = ((1.0f + scales[index]) * values[index]) + shifts[index];
            }
            return output;
        }
        output.Dispose();
        throw new NotSupportedException(
            $"AdaLN modulation is not implemented for {input.DataType}.");
    }
}

public sealed class OwnedVibeVoiceDiffusionHead : IDisposable
{
    private readonly Dictionary<string, Tensor> _state;
    private bool _disposed;

    internal OwnedVibeVoiceDiffusionHead(
        VibeVoiceDiffusionHead diffusionHead,
        Dictionary<string, Tensor> state)
    {
        Head = diffusionHead;
        _state = state;
    }

    public VibeVoiceDiffusionHead Head { get; }

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
