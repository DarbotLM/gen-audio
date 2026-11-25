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
    long TokenizerVocabularySize,
    VibeVoiceSpecialTokens SpecialTokens,
    IReadOnlyList<VibeVoiceWaveInfo> References,
    int PromptTokenCount,
    int PromptSpeechSlots,
    VibeVoiceGraphPlan Graph,
    IReadOnlyList<VibeVoiceCapability> Implemented,
    IReadOnlyList<VibeVoiceCapability> Missing);

public sealed record VibeVoiceSyntheticReceipt(
    bool Ok,
    string Test,
    IReadOnlyList<long> Shape,
    string DataType,
    bool AllFinite,
    bool? TokenizerValid,
    int PromptTokenCount,
    bool? SpeakerConfinementValid,
    bool WaveValidationValid,
    IReadOnlyList<int> ResampledRates);

public static class VibeVoiceRuntime
{
    public static VibeVoiceCapabilityReceipt Inspect(
        string modelDirectory,
        string? tokenizerDirectory = null,
        string? script = null,
        string? voiceRoot = null,
        IReadOnlyList<string>? voiceReferences = null)
    {
        string root = Path.GetFullPath(modelDirectory);
        tokenizerDirectory = Path.GetFullPath(
            tokenizerDirectory ?? Path.Combine(root, "tokenizer"));
        Qwen2BpeTokenizer tokenizer = Qwen2BpeTokenizer.Load(tokenizerDirectory);
        var references = new List<Pcm16WaveFile>();
        string[] referenceNames = [.. voiceReferences ?? []];
        if (referenceNames.Length > 4)
        {
            throw new ArgumentException("VibeVoice inspection accepts at most 4 references.");
        }
        foreach (string reference in referenceNames)
        {
            string resolved = Path.IsPathFullyQualified(reference)
                ? reference
                : Path.Combine(voiceRoot ?? root, reference);
            references.Add(Pcm16WaveFile.Read(resolved, voiceRoot));
        }
        if (script is not null && (references.Count == 0 || references.Count > 4))
        {
            throw new ArgumentException(
                "Script inspection requires 1..4 confined reference WAV files.");
        }
        VibeVoicePrompt? prompt = script is null
            ? null
            : tokenizer.TokenizeScript(
                script,
                references.Select(reference => reference.Samples24Khz).ToArray());
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
        AddIntrinsic(implemented, "text.qwen2_bpe_tokenizer",
            "WingenAudio.VibeVoice BPE, vocab, merges, special-token, and script prompt loader");
        AddIntrinsic(implemented, "audio.wave_io",
            "bounded mono PCM16 WAV loader with 24 kHz linear resampling");
        foreach (string capability in required)
        {
            if (capability is "text.qwen2_bpe_tokenizer" or "audio.wave_io")
            {
                continue;
            }
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
            TokenizerVocabularySize: tokenizer.VocabularySize,
            SpecialTokens: new(
                tokenizer.EndOfTextId,
                tokenizer.SpeechStartId,
                tokenizer.SpeechEndId,
                tokenizer.SpeechDiffusionId),
            References: references.Select(reference => reference.Info).ToArray(),
            PromptTokenCount: prompt?.InputIds.Count ?? 0,
            PromptSpeechSlots: prompt?.SpeechInputMask.Count(value => value) ?? 0,
            Graph: graph,
            Implemented: implemented,
            Missing: missing);
    }

    public static VibeVoiceSyntheticReceipt RunSyntheticValidation(
        string? tokenizerDirectory = null)
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
            bool? tokenizerValid = null;
            int promptTokenCount = 0;
            bool? speakerConfinementValid = null;
            if (tokenizerDirectory is not null)
            {
                Qwen2BpeTokenizer tokenizer = Qwen2BpeTokenizer.Load(tokenizerDirectory);
                int[] directA = tokenizer.Encode("A");
                int[] directHello = tokenizer.Encode("Hello");
                int[] prefixedHello = tokenizer.Encode(" Hello");
                int[] toolCall = tokenizer.Encode("<tool_call>");
                VibeVoicePrompt prompt = tokenizer.TokenizeScript(
                    "Speaker 1: Hello.\nSpeaker 2: Testing.",
                    [new float[12000], new float[12000]]);
                tokenizerValid =
                    directA.SequenceEqual([32]) &&
                    directHello.SequenceEqual([9707]) &&
                    prefixedHello.SequenceEqual([21927]) &&
                    toolCall.SequenceEqual([151657]) &&
                    tokenizer.VocabularySize == 151665 &&
                    tokenizer.SpeechStartId == 151652 &&
                    tokenizer.SpeechEndId == 151653 &&
                    tokenizer.SpeechDiffusionId == 151654 &&
                    tokenizer.EndOfTextId == 151643 &&
                    prompt.Speakers.Count == 2 &&
                    prompt.InputIds.Count == prompt.SpeechInputMask.Count &&
                    prompt.InputIds[^1] == tokenizer.SpeechStartId &&
                    prompt.SpeechInputMask.Count(value => value) == 8;
                promptTokenCount = prompt.InputIds.Count;
                try
                {
                    VibeVoiceSpeakerScript.Parse("Speaker 5: no");
                }
                catch (ArgumentException)
                {
                    speakerConfinementValid = true;
                }
                speakerConfinementValid = speakerConfinementValid is not false &&
                    VibeVoiceSpeakerScript.Parse(
                        "Speaker 1: one\nSpeaker 2: two\nSpeaker 3: three\nSpeaker 4: four")
                    .Speakers.Count == 4;
            }
            int[] rates = [16000, 22050, 24000, 44100, 48000];
            var resampledRates = new List<int>();
            bool waveValid = true;
            foreach (int rate in rates)
            {
                using MemoryStream wavStream = CreateSyntheticWave(rate);
                Pcm16WaveFile wave = Pcm16WaveFile.Read(
                    wavStream,
                    $"synthetic-{rate}.wav");
                bool expected =
                    wave.Info.SampleRate == rate &&
                    wave.Info.TargetSampleRate == 24000 &&
                    wave.Info.DurationSeconds >= 0.5 &&
                    wave.Samples24Khz.Length == 12000 &&
                    wave.Samples24Khz.All(float.IsFinite) &&
                    wave.Info.Peak > 0;
                waveValid &= expected;
                if (expected)
                {
                    resampledRates.Add(rate);
                }
            }
            try
            {
                using MemoryStream shortWave = CreateSyntheticWave(24000, 0.49);
                Pcm16WaveFile.Read(shortWave, "synthetic-short.wav");
                waveValid = false;
            }
            catch (InvalidDataException)
            {
            }
            try
            {
                using MemoryStream longWave = CreateSyntheticWave(24000, 30.01);
                Pcm16WaveFile.Read(longWave, "synthetic-long.wav");
                waveValid = false;
            }
            catch (InvalidDataException)
            {
            }
            try
            {
                Pcm16WaveFile.Read(
                    Path.Combine(
                        Directory.GetCurrentDirectory(),
                        "..",
                        "outside.wav"),
                    Directory.GetCurrentDirectory());
                waveValid = false;
            }
            catch (IOException)
            {
            }
            return new(
                Ok: finite &&
                    tokenizerValid is not false &&
                    speakerConfinementValid is not false &&
                    waveValid &&
                    output.Shape.SequenceEqual(new long[] { 1, 3, hidden }),
                Test: "qwen2-prefill-and-native-tokenizer-wav-boundary",
                Shape: output.Shape,
                DataType: output.DataType.ToString(),
                AllFinite: finite,
                TokenizerValid: tokenizerValid,
                PromptTokenCount: promptTokenCount,
                SpeakerConfinementValid: speakerConfinementValid,
                WaveValidationValid: waveValid,
                ResampledRates: resampledRates);
        }

        finally
        {
            foreach (Tensor tensor in state.Values)
            {
                tensor.Dispose();
            }
        }
    }

    private static void AddIntrinsic(
        ICollection<VibeVoiceCapability> capabilities,
        string name,
        string boundary) =>
        capabilities.Add(new(name, true, boundary));

    private static MemoryStream CreateSyntheticWave(
        int sampleRate,
        double durationSeconds = 0.5)
    {
        int samples = checked((int)Math.Round(sampleRate * durationSeconds));
        var stream = new MemoryStream();
        using var writer = new BinaryWriter(stream, System.Text.Encoding.ASCII, true);
        writer.Write("RIFF".ToCharArray());
        writer.Write(36 + (samples * 2));
        writer.Write("WAVE".ToCharArray());
        writer.Write("fmt ".ToCharArray());
        writer.Write(16);
        writer.Write((short)1);
        writer.Write((short)1);
        writer.Write(sampleRate);
        writer.Write(sampleRate * 2);
        writer.Write((short)2);
        writer.Write((short)16);
        writer.Write("data".ToCharArray());
        writer.Write(samples * 2);
        for (int index = 0; index < samples; index++)
        {
            writer.Write((short)((index & 1) == 0 ? 8192 : -8192));
        }
        writer.Flush();
        stream.Position = 0;
        return stream;
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
