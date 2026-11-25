using PowerTorch;
using PowerTorch.Cpu;
using PowerTorch.Core;
using WingenAudio.Contracts;

namespace WingenAudio.VibeVoice;

public sealed record VibeVoiceCapabilityReceipt(
    string SchemaVersion,
    string Engine,
    string Capability,
    bool Ok,
    bool Staged,
    bool GenerationReady,
    bool SynthesizedSpeech,
    string Runtime,
    string Backend,
    string ModelDirectory,
    long TensorCount,
    long ShardCount,
    long StateBytes,
    long DecoderBytes,
    VibeVoiceGraphPlan Graph,
    IReadOnlyList<VibeVoiceCapability> Implemented,
    IReadOnlyList<VibeVoiceCapability> Missing);

public sealed record VibeVoiceSyntheticReceipt(
    bool Ok,
    string Test,
    IReadOnlyList<long> Shape,
    string DataType,
    bool AllFinite);

public static class VibeVoiceRuntime
{
    public static VibeVoiceCapabilityReceipt Inspect(string modelDirectory)
    {
        string root = Path.GetFullPath(modelDirectory);
        VibeVoiceConfiguration configuration = VibeVoiceConfiguration.Load(root);
        using VibeVoiceStateMap state = VibeVoiceStateMap.Open(root, configuration);
        VibeVoiceGraphPlan graph = VibeVoiceGraph.Build(configuration, state);
        var backend = new CpuBackend();
        IReadOnlyDictionary<string, bool> features =
            backend.GetCapabilities().Features;
        string[] required =
        [
            "tensor.matmul",
            "tensor.reshape",
            "tensor.transpose",
            "tensor.embedding",
            "tensor.rms_norm",
            "tensor.rotary",
            "tensor.causal_attention",
            "tensor.conv1d",
            "model.safe_state_load",
            "model.sharded_checkpoint",
            "tensor.concat",
            "tensor.kv_cache_append",
            "tensor.arg_max",
            "tensor.random_normal",
            "tensor.trigonometric",
            "text.qwen2_bpe_tokenizer",
            "audio.wave_io",
            "audio.acoustic_tokenizer.encoder",
            "audio.acoustic_tokenizer.decoder",
            "audio.semantic_tokenizer.encoder",
            "audio.diffusion_scheduler",
            "audio.streaming_cache",
            "model.generation",
            "device.transfer",
            "device.tensor_residency",
        ];
        var implemented = new List<VibeVoiceCapability>();
        var missing = new List<VibeVoiceCapability>();
        foreach (string capability in required)
        {
            bool available = features.TryGetValue(capability, out bool supported) &&
                supported;
            var item = new VibeVoiceCapability(
                capability,
                available,
                available
                    ? "available from the pinned PowerTorch CPU backend"
                    : "required for production VibeVoice generation but not available");
            (available ? implemented : missing).Add(item);
        }
        return new(
            SchemaVersion: "1.0",
            Engine: "vibevoice",
            Capability: "qwen2-prefill-and-vibevoice-generation-plan",
            Ok: missing.Count == 0,
            Staged: true,
            GenerationReady: false,
            SynthesizedSpeech: false,
            Runtime: "powertorch",
            Backend: "cpu",
            ModelDirectory: root,
            TensorCount: state.Entries.Count,
            ShardCount: state.ShardPaths.Count,
            StateBytes: state.TotalBytes,
            DecoderBytes: state.DecoderBytes,
            Graph: graph,
            Implemented: implemented,
            Missing: missing);
    }

    public static VibeVoiceSyntheticReceipt RunSyntheticValidation()
    {
        const int hidden = 4;
        const int intermediate = 8;
        var configuration = new Qwen2DecoderConfiguration(
            hidden,
            intermediate,
            1,
            2,
            1,
            32,
            32,
            1e-6,
            10_000,
            "silu",
            false);
        var state = new Dictionary<string, Tensor>(StringComparer.Ordinal);
        try
        {
            Qwen2TensorNames names = Qwen2TensorNames.ForLayer(0);
            AddFilled(state, names.InputNorm, 1, 4);
            AddFilled(state, names.QueryWeight, 0.05, 4, 4);
            AddFilled(state, names.QueryBias!, 0.01, 4);
            AddFilled(state, names.KeyWeight, 0.05, 2, 4);
            AddFilled(state, names.KeyBias!, 0.01, 2);
            AddFilled(state, names.ValueWeight, 0.05, 2, 4);
            AddFilled(state, names.ValueBias!, 0.01, 2);
            AddFilled(state, names.OutputWeight, 0.05, 4, 4);
            AddFilled(state, names.PostAttentionNorm, 1, 4);
            AddFilled(state, names.GateWeight, 0.05, intermediate, hidden);
            AddFilled(state, names.UpWeight, 0.05, intermediate, hidden);
            AddFilled(state, names.DownWeight, 0.05, hidden, intermediate);
            var backend = new CpuBackend();
            var layer = new Qwen2DecoderLayer(
                backend,
                configuration,
                names,
                state);
            using Tensor input = TensorFactory.FromArray(
                Enumerable.Range(0, 12)
                    .Select(value => BFloat16.FromSingle(value / 20f))
                    .ToArray(),
                [1, 3, hidden]);
            using Tensor positions = TensorFactory.FromArray(
                new long[] { 0, 1, 2 },
                [3]);
            using Tensor output = layer.Forward(input, positions);
            bool finite = output.AsSpan<BFloat16>().ToArray()
                .All(value => float.IsFinite(value));
            return new(
                Ok: finite && output.Shape.SequenceEqual(new long[] { 1, 3, hidden }),
                Test: "qwen2-pre-fill-gqa-rmsnorm-swiglu-rope-causal-attention",
                Shape: output.Shape,
                DataType: output.DataType.ToString(),
                AllFinite: finite);
        }
        finally
        {
            foreach (Tensor tensor in state.Values)
            {
                tensor.Dispose();
            }
        }
    }

    private static void AddFilled(
        IDictionary<string, Tensor> state,
        string name,
        double value,
        params long[] shape)
    {
        long count = shape.Aggregate(1L, (left, right) => checked(left * right));
        state.Add(
            name,
            TensorFactory.FromArray(
                Enumerable.Range(0, checked((int)count))
                    .Select(index => (BFloat16)(float)(
                        value + ((index % 3) - 1) * 0.001))
                    .ToArray(),
                shape));
    }
}
