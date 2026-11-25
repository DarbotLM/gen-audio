using PowerTorch;
using PowerTorch.Core;
using PowerTorch.Serialization;

namespace WingenAudio.VibeVoice;

public enum VibeVoiceStateRegion
{
    TextDecoder,
    LanguageHead,
    AcousticTokenizer,
    SemanticTokenizer,
    AcousticConnector,
    SemanticConnector,
    DiffusionHead,
    RuntimeBuffer,
}

public sealed record VibeVoiceStateEntry(
    string Name,
    VibeVoiceStateRegion Region,
    DataType DataType,
    IReadOnlyList<long> Shape,
    long Bytes);

public sealed class VibeVoiceStateMap : IDisposable
{
    private readonly ShardedSafetensorsCheckpoint _checkpoint;
    private bool _disposed;

    private VibeVoiceStateMap(
        ShardedSafetensorsCheckpoint checkpoint,
        VibeVoiceConfiguration configuration)
    {
        _checkpoint = checkpoint;
        Configuration = configuration;
        Entries = checkpoint.TensorNames
            .Select(name =>
            {
                var descriptor = checkpoint.GetTensor(name);
                return new VibeVoiceStateEntry(
                    name,
                    Classify(name),
                    descriptor.DataType,
                    descriptor.Shape,
                    descriptor.ByteLength);
            })
            .ToArray();
    }

    public VibeVoiceConfiguration Configuration { get; }

    public IReadOnlyList<VibeVoiceStateEntry> Entries { get; }

    public IReadOnlyList<string> ShardPaths => _checkpoint.ShardPaths;

    public long TotalBytes => Entries.Sum(entry => entry.Bytes);

    public long DecoderBytes => Entries
        .Where(entry => entry.Region is VibeVoiceStateRegion.TextDecoder or VibeVoiceStateRegion.LanguageHead)
        .Sum(entry => entry.Bytes);

    public static VibeVoiceStateMap Open(
        string modelDirectory,
        VibeVoiceConfiguration? configuration = null)
    {
        string root = Path.GetFullPath(modelDirectory);
        configuration ??= VibeVoiceConfiguration.Load(root);
        string indexPath = Path.Combine(root, "model.safetensors.index.json");
        var checkpoint = new ShardedSafetensorsCheckpoint(indexPath);
        try
        {
            var map = new VibeVoiceStateMap(checkpoint, configuration);
            map.Validate();
            return map;
        }
        catch
        {
            checkpoint.Dispose();
            throw;
        }
    }

    public VibeVoiceStateEntry Require(string name)
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        VibeVoiceStateEntry? entry = Entries.FirstOrDefault(candidate =>
            string.Equals(candidate.Name, name, StringComparison.Ordinal));
        return entry ?? throw new InvalidDataException($"Checkpoint is missing tensor '{name}'.");
    }

    public async ValueTask<Tensor> LoadTensorAsync(
        string name,
        long maximumBytes = 1_000_000_000,
        CancellationToken cancellationToken = default)
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        Require(name);
        return await _checkpoint.LoadTensorAsync(name, maximumBytes, cancellationToken);
    }

    public async ValueTask<Dictionary<string, Tensor>> LoadLayerAsync(
        int layer,
        CancellationToken cancellationToken = default)
    {
        if (layer < 0 || layer >= Configuration.Decoder.Layers)
        {
            throw new ArgumentOutOfRangeException(nameof(layer));
        }
        string prefix = $"model.language_model.layers.{layer}.";
        string[] names = Entries
            .Where(entry => entry.Name.StartsWith(prefix, StringComparison.Ordinal))
            .Select(entry => entry.Name)
            .ToArray();
        var state = new Dictionary<string, Tensor>(StringComparer.Ordinal);
        try
        {
            foreach (string name in names)
            {
                state.Add(name, await LoadTensorAsync(name, cancellationToken: cancellationToken));
            }
            return state;
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

    public IReadOnlyList<string> Validate()
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        var errors = new List<string>();
        RequireDecoderShape(
            "model.language_model.embed_tokens.weight",
            [Configuration.Decoder.VocabularySize, Configuration.Decoder.HiddenSize],
            errors);
        RequireDecoderShape(
            "model.language_model.norm.weight",
            [Configuration.Decoder.HiddenSize],
            errors);
        RequireDecoderShape(
            "lm_head.weight",
            [Configuration.Decoder.VocabularySize, Configuration.Decoder.HiddenSize],
            errors);
        for (int layer = 0; layer < Configuration.Decoder.Layers; layer++)
        {
            string prefix = $"model.language_model.layers.{layer}.";
            RequireDecoderShape(prefix + "input_layernorm.weight", [Configuration.Decoder.HiddenSize], errors);
            RequireDecoderShape(prefix + "post_attention_layernorm.weight", [Configuration.Decoder.HiddenSize], errors);
            RequireDecoderShape(
                prefix + "self_attn.q_proj.weight",
                [Configuration.Decoder.HiddenSize, Configuration.Decoder.HiddenSize],
                errors);
            RequireDecoderShape(
                prefix + "self_attn.q_proj.bias",
                [Configuration.Decoder.HiddenSize],
                errors);
            RequireDecoderShape(
                prefix + "self_attn.k_proj.weight",
                [Configuration.Decoder.KeyValueHeadDimension(), Configuration.Decoder.HiddenSize],
                errors);
            RequireDecoderShape(
                prefix + "self_attn.k_proj.bias",
                [Configuration.Decoder.KeyValueHeadDimension()],
                errors);
            RequireDecoderShape(
                prefix + "self_attn.v_proj.weight",
                [Configuration.Decoder.KeyValueHeadDimension(), Configuration.Decoder.HiddenSize],
                errors);
            RequireDecoderShape(
                prefix + "self_attn.v_proj.bias",
                [Configuration.Decoder.KeyValueHeadDimension()],
                errors);
            RequireDecoderShape(
                prefix + "self_attn.o_proj.weight",
                [Configuration.Decoder.HiddenSize, Configuration.Decoder.HiddenSize],
                errors);
            RequireDecoderShape(
                prefix + "mlp.gate_proj.weight",
                [Configuration.Decoder.IntermediateSize, Configuration.Decoder.HiddenSize],
                errors);
            RequireDecoderShape(
                prefix + "mlp.up_proj.weight",
                [Configuration.Decoder.IntermediateSize, Configuration.Decoder.HiddenSize],
                errors);
            RequireDecoderShape(
                prefix + "mlp.down_proj.weight",
                [Configuration.Decoder.HiddenSize, Configuration.Decoder.IntermediateSize],
                errors);
        }
        RequireRegion(VibeVoiceStateRegion.AcousticTokenizer, "acoustic tokenizer", errors);
        RequireRegion(VibeVoiceStateRegion.SemanticTokenizer, "semantic tokenizer", errors);
        RequireRegion(VibeVoiceStateRegion.AcousticConnector, "acoustic connector", errors);
        RequireRegion(VibeVoiceStateRegion.SemanticConnector, "semantic connector", errors);
        RequireRegion(VibeVoiceStateRegion.DiffusionHead, "diffusion head", errors);
        for (int layer = 0; layer < Configuration.DiffusionHead.Layers; layer++)
        {
            RequireDecoderShape(
                $"model.prediction_head.layers.{layer}.norm.weight",
                [Configuration.DiffusionHead.HiddenSize],
                errors);
        }
        foreach (VibeVoiceStateEntry entry in Entries.Where(candidate =>
                     candidate.Region != VibeVoiceStateRegion.RuntimeBuffer &&
                     candidate.DataType != DataType.BFloat16))
        {
            errors.Add($"Tensor '{entry.Name}' is {entry.DataType}, expected BF16.");
        }
        if (errors.Count != 0)
        {
            throw new InvalidDataException(string.Join(Environment.NewLine, errors));
        }
        return [];
    }

    private void RequireDecoderShape(
        string name,
        IReadOnlyList<long> expected,
        ICollection<string> errors)
    {
        VibeVoiceStateEntry? entry = Entries.FirstOrDefault(candidate =>
            string.Equals(candidate.Name, name, StringComparison.Ordinal));
        if (entry is null)
        {
            errors.Add($"Missing tensor '{name}'.");
        }

        else if (!entry.Shape.SequenceEqual(expected))
        {
            errors.Add(
                $"Tensor '{name}' has shape [{string.Join(',', entry.Shape)}], expected [{string.Join(',', expected)}].");
        }
    }

    private void RequireRegion(
        VibeVoiceStateRegion region,
        string displayName,
        ICollection<string> errors)
    {
        if (!Entries.Any(entry => entry.Region == region))
        {
            errors.Add($"Checkpoint is missing all {displayName} tensors.");
        }
    }

    private static VibeVoiceStateRegion Classify(string name)
    {
        if (name.StartsWith("model.language_model.", StringComparison.Ordinal))
        {
            return VibeVoiceStateRegion.TextDecoder;
        }
        if (name.StartsWith("lm_head.", StringComparison.Ordinal))
        {
            return VibeVoiceStateRegion.LanguageHead;
        }
        if (name.StartsWith("model.acoustic_tokenizer.", StringComparison.Ordinal))
        {
            return VibeVoiceStateRegion.AcousticTokenizer;
        }
        if (name.StartsWith("model.semantic_tokenizer.", StringComparison.Ordinal))
        {
            return VibeVoiceStateRegion.SemanticTokenizer;
        }
        if (name.StartsWith("model.acoustic_connector.", StringComparison.Ordinal))
        {
            return VibeVoiceStateRegion.AcousticConnector;
        }
        if (name.StartsWith("model.semantic_connector.", StringComparison.Ordinal))
        {
            return VibeVoiceStateRegion.SemanticConnector;
        }
        if (name.StartsWith("model.prediction_head.", StringComparison.Ordinal))
        {
            return VibeVoiceStateRegion.DiffusionHead;
        }
        return VibeVoiceStateRegion.RuntimeBuffer;
    }

    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }
        _disposed = true;
        _checkpoint.Dispose();
    }
}

internal static class QwenConfigurationExtensions
{
    public static int KeyValueHeadDimension(this Qwen2DecoderConfiguration configuration) =>
        configuration.HiddenSize / configuration.AttentionHeads * configuration.KeyValueHeads;
}
