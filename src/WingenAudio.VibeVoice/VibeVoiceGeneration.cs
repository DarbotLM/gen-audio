using System.Security.Cryptography;
using System.Text.Json;
using PowerTorch;
using PowerTorch.Core;

namespace WingenAudio.VibeVoice;

public sealed record VibeVoiceGenerationOptions(
    string ModelDirectory,
    string TokenizerDirectory,
    string VoiceRoot,
    string Script,
    IReadOnlyList<string> VoiceReferences,
    string OutputPath,
    int MaxNewTokens = 256,
    double MaxSeconds = 30,
    double CfgScale = 1.3,
    int InferenceSteps = 20,
    int Seed = 0,
    double TimeoutSeconds = 600);

public sealed record VibeVoiceGenerationReceipt(
    string SchemaVersion,
    bool Ok,
    string Engine,
    string Runtime,
    bool SynthesizedSpeech,
    string Output,
    string Manifest,
    string Sha256,
    int SampleRate,
    int Channels,
    long Samples,
    double DurationSeconds,
    int GeneratedTokens,
    int DiffusionFrames,
    string StopReason,
    long StateBytes,
    double ElapsedSeconds);

public sealed class VibeVoiceGenerator : IAsyncDisposable
{
    private readonly VibeVoiceConfiguration _configuration;
    private readonly Dictionary<string, Tensor> _state;
    private readonly CpuGenerationComponents _components;
    private bool _disposed;

    private VibeVoiceGenerator(
        VibeVoiceConfiguration configuration,
        Dictionary<string, Tensor> state,
        CpuGenerationComponents components)
    {
        _configuration = configuration;
        _state = state;
        _components = components;
    }

    public static async ValueTask<VibeVoiceGenerator> LoadAsync(
        string modelDirectory,
        CancellationToken cancellationToken = default)
    {
        VibeVoiceConfiguration configuration =
            VibeVoiceConfiguration.Load(modelDirectory);
        using VibeVoiceStateMap stateMap =
            VibeVoiceStateMap.Open(modelDirectory, configuration);
        Dictionary<string, Tensor> state =
            await stateMap.LoadAllAsync(cancellationToken);
        try
        {
            var backend = new PowerTorch.Cpu.CpuBackend();
            var components = new CpuGenerationComponents(
                backend,
                configuration,
                state);
            return new(configuration, state, components);
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

    public async ValueTask<VibeVoiceGenerationReceipt> GenerateAsync(
        VibeVoiceGenerationOptions options,
        CancellationToken cancellationToken = default)
    {
        ValidateOptions(options);
        var stopwatch = System.Diagnostics.Stopwatch.StartNew();
        Qwen2BpeTokenizer tokenizer =
            Qwen2BpeTokenizer.Load(options.TokenizerDirectory);
        Pcm16WaveFile[] references = options.VoiceReferences
            .Select(reference => Pcm16WaveFile.Read(
                Path.IsPathFullyQualified(reference)
                    ? reference
                    : Path.Combine(options.VoiceRoot, reference),
                options.VoiceRoot))
            .ToArray();
        VibeVoiceSpeakerScript script = VibeVoiceSpeakerScript.Parse(options.Script);
        int speakers = script.Speakers
            .Select(line => line.Speaker)
            .Distinct()
            .Count();
        if (speakers != references.Length)
        {
            throw new ArgumentException(
                "Reference WAV count must equal the distinct speaker count.");
        }

        VibeVoicePrompt prompt = tokenizer.TokenizeScript(
            options.Script,
            references.Select(reference => reference.Samples24Khz).ToArray());
        using Tensor promptIds = TensorFactory.FromArray(
            prompt.InputIds.Select(value => (long)value).ToArray(),
            [1, prompt.InputIds.Count]);
        using Tensor promptEmbeddings = _components.Decoder.Embed(promptIds);
        await ReplaceReferenceEmbeddingsAsync(
            promptEmbeddings,
            prompt,
            references,
            options.InferenceSteps,
            cancellationToken);
        using Tensor positions = TensorFactory.FromArray(
            Enumerable.Range(0, prompt.InputIds.Count)
                .Select(value => (long)value)
                .ToArray(),
            [prompt.InputIds.Count]);
        using var positiveCache =
            new Qwen2DecoderCache(_configuration.Decoder.Layers);
        using Qwen2DecoderStep prefill =
            _components.Decoder.ForwardCached(
                promptEmbeddings,
                positions,
                positiveCache);

        using Tensor negativeStart = TensorFactory.FromArray(
            new long[] { tokenizer.SpeechStartId },
            [1, 1]);
        using Tensor negativeEmbeddings = _components.Decoder.Embed(negativeStart);
        using Tensor zeroPosition = TensorFactory.FromArray(
            new long[] { 0 },
            [1]);
        using var negativeCache =
            new Qwen2DecoderCache(_configuration.Decoder.Layers);
        using Qwen2DecoderStep negativePrefill =
            _components.Decoder.ForwardCached(
                negativeEmbeddings,
                zeroPosition,
                negativeCache);

        var audioChunks = new List<float[]>();
        var streamedLatents = new List<BFloat16>();
        int decodedSamples = 0;
        int generatedTokens = 0;
        int diffusionFrames = 0;
        long sequencePosition = prompt.InputIds.Count;
        long negativePosition = 1;
        string stopReason = "max_tokens";
        Tensor positiveHidden = prefill.Output.Clone();
        Tensor negativeHidden = negativePrefill.Output.Clone();
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(
            cancellationToken);
        deadline.CancelAfter(TimeSpan.FromSeconds(options.TimeoutSeconds));
        try
        {
            while (generatedTokens < options.MaxNewTokens)
            {
                deadline.Token.ThrowIfCancellationRequested();
                using Tensor positiveLast = LastHidden(positiveHidden);
                using Tensor positiveLogits = TensorLinear.Forward(
                    _components.Backend,
                    positiveLast,
                    _components.LanguageHead);
                int token = SelectAllowedToken(positiveLogits, tokenizer);
                generatedTokens++;
                if (token == tokenizer.EndOfTextId)
                {
                    stopReason = "end_of_text";
                    break;
                }
                Tensor nextEmbedding;
                if (token == tokenizer.SpeechDiffusionId)
                {
                    diffusionFrames++;
                    using Tensor positiveCondition = LastHidden(positiveHidden);
                    using Tensor negativeCondition = LastHidden(negativeHidden);
                    using Tensor generatedLatent = SampleLatent(
                        positiveCondition,
                        negativeCondition,
                        options,
                        diffusionFrames,
                        deadline.Token);
                    using Tensor scaledLatent =
                        InvertSpeechScaling(generatedLatent);
                    streamedLatents.AddRange(
                        scaledLatent.AsSpan<BFloat16>().ToArray());
                    using Tensor latentStream =
                        TensorFactory.FromArray(
                            streamedLatents.ToArray(),
                            [
                                1,
                                streamedLatents.Count /
                                    _configuration.DiffusionHead.LatentSize,
                                _configuration.DiffusionHead.LatentSize,
                            ]);
                    using Tensor decodedStream =
                        _components.Acoustic.Decode(latentStream);
                    float[] allAudio = decodedStream.AsSpan<BFloat16>()
                        .ToArray()
                        .Select(value => (float)value)
                        .ToArray();
                    float[] chunk = allAudio.Skip(decodedSamples).ToArray();
                    decodedSamples = allAudio.Length;
                    audioChunks.Add(chunk);
                    if (audioChunks.Sum(value => (long)value.Length) >=
                        options.MaxSeconds * 24000)
                    {
                        stopReason = "max_seconds";
                    }
                    using Tensor semanticLatent =
                        _components.Semantic.Encode(decodedStream);
                    using Tensor semanticCurrent =
                        LastSequenceValue(semanticLatent);
                    using Tensor acousticEmbedding =
                        _components.AcousticConnector.Forward(generatedLatent);
                    using Tensor semanticEmbedding =
                        _components.SemanticConnector.Forward(semanticCurrent);
                    nextEmbedding = _components.Backend.Add(
                        acousticEmbedding,
                        semanticEmbedding);
                }
                else
                {
                    using Tensor tokenIds = TensorFactory.FromArray(
                        new long[] { token },
                        [1, 1]);
                    nextEmbedding = _components.Decoder.Embed(tokenIds);
                }

                try
                {
                    using Tensor nextPosition = TensorFactory.FromArray(
                        new long[] { sequencePosition++ },
                        [1]);
                    using Qwen2DecoderStep positiveStep =
                        _components.Decoder.ForwardCached(
                            nextEmbedding,
                            nextPosition,
                            positiveCache);
                    positiveHidden.Dispose();
                    positiveHidden = positiveStep.Output.Clone();

                    using Tensor negativeIds = TensorFactory.FromArray(
                        new long[] { tokenizer.SpeechDiffusionId },
                        [1, 1]);
                    using Tensor negativeEmbedding =
                        _components.Decoder.Embed(negativeIds);
                    using Tensor nextNegativePosition =
                        TensorFactory.FromArray(
                            new long[] { negativePosition++ },
                            [1]);
                    using Qwen2DecoderStep negativeStep =
                        _components.Decoder.ForwardCached(
                            negativeEmbedding,
                            nextNegativePosition,
                            negativeCache);
                    negativeHidden.Dispose();
                    negativeHidden = negativeStep.Output.Clone();
                }
                finally
                {
                    nextEmbedding.Dispose();
                }
                if (stopReason == "max_seconds")
                {
                    break;
                }
            }
        }
        finally
        {
            positiveHidden.Dispose();
            negativeHidden.Dispose();
        }

        float[] samples = audioChunks
            .SelectMany(chunk => chunk)
            .Take(checked((int)Math.Round(options.MaxSeconds * 24000)))
            .ToArray();
        if (samples.Length == 0)
        {
            throw new InvalidOperationException(
                "Generation completed without an acoustic diffusion frame.");
        }
        string output = PublishWave(options.OutputPath, samples);
        string sha256 = await Sha256Async(output, cancellationToken);
        string manifest = output + ".manifest.json";
        var manifestReceipt = new VibeVoiceGenerationReceipt(
            SchemaVersion: "1.0",
            Ok: true,
            Engine: "vibevoice",
            Runtime: "powertorch-cpu",
            SynthesizedSpeech: true,
            Output: output,
            Manifest: manifest,
            Sha256: sha256,
            SampleRate: 24000,
            Channels: 1,
            Samples: samples.Length,
            DurationSeconds: samples.Length / 24000.0,
            GeneratedTokens: generatedTokens,
            DiffusionFrames: diffusionFrames,
            StopReason: stopReason,
            StateBytes: _state.Values.Sum(value => value.Descriptor.ByteLength),
            ElapsedSeconds: stopwatch.Elapsed.TotalSeconds);
        await File.WriteAllTextAsync(
            manifest,
            JsonSerializer.Serialize(
                manifestReceipt,
                new JsonSerializerOptions(JsonSerializerDefaults.Web)
                {
                    WriteIndented = true,
                }),
            cancellationToken);
        return manifestReceipt;
    }

    public static bool ValidateSyntheticPublication(string repoRoot)
    {
        string directory = Path.Combine(
            Path.GetFullPath(repoRoot),
            "artifacts",
            "vibevoice-synthetic");
        Directory.CreateDirectory(directory);
        string output = Path.Combine(
            directory,
            $"synthetic-{Guid.NewGuid():N}.wav");
        string manifest = output + ".manifest.json";
        try
        {
            float[] samples = Enumerable.Range(0, 12000)
                .Select(index => MathF.Sin(index / 20f) * 0.125f)
                .ToArray();
            string path = PublishWave(output, samples);
            Pcm16WaveFile wave = Pcm16WaveFile.Read(path, directory);
            byte[] hash = SHA256.HashData(File.ReadAllBytes(path));
            return wave.Info.SampleRate == 24000 &&
                wave.Info.TargetSampleRate == 24000 &&
                wave.Info.DurationSeconds == 0.5 &&
                hash.Length == 32;
        }
        finally
        {
            if (File.Exists(output))
            {
                File.Delete(output);
            }
            if (File.Exists(manifest))
            {
                File.Delete(manifest);
            }
        }
    }

    private int SelectAllowedToken(
        Tensor logits,
        Qwen2BpeTokenizer tokenizer)
    {
        int[] allowed =
        [
            tokenizer.SpeechStartId,
            tokenizer.SpeechEndId,
            tokenizer.SpeechDiffusionId,
            tokenizer.EndOfTextId,
        ];
        ReadOnlySpan<BFloat16> values = logits.AsSpan<BFloat16>();
        int selected = allowed[0];
        float score = float.NegativeInfinity;
        foreach (int id in allowed)
        {
            float current = values[id];
            if (current > score)
            {
                score = current;
                selected = id;
            }
        }
        return selected;
    }

    private Tensor SampleLatent(
        Tensor condition,
        Tensor negativeCondition,
        VibeVoiceGenerationOptions options,
        int frame,
        CancellationToken cancellationToken)
    {
        var scheduler = new CosineDdpmScheduler(
            _components.Backend,
            _configuration.DiffusionHead.TrainingSteps,
            _configuration.DiffusionHead.PredictionType);
        int[] timesteps = scheduler.SetTimesteps(options.InferenceSteps);
        using Tensor noise = _components.Backend.RandomNormal(
            DataType.BFloat16,
            [1, _configuration.DiffusionHead.LatentSize],
            0,
            1,
            options.Seed + frame);
        Tensor sample = noise.Clone();
        try
        {
            for (int index = 0; index < timesteps.Length; index++)
            {
                cancellationToken.ThrowIfCancellationRequested();
                using Tensor currentTimestep = TensorFactory.FromArray(
                    new long[] { timesteps[index] },
                    [1]);
                using Tensor prediction =
                    _components.Diffusion.PredictWithClassifierFreeGuidance(
                        sample,
                        currentTimestep,
                        condition,
                        negativeCondition,
                        options.CfgScale);
                int? previous = index + 1 < timesteps.Length
                    ? timesteps[index + 1]
                    : -1;
                Tensor next = scheduler.Step(
                    prediction,
                    currentTimestep,
                    sample,
                    noise: null,
                    previous);
                sample.Dispose();
                sample = next;
            }
            return sample;
        }
        catch
        {
            sample.Dispose();
            throw;
        }
    }

    private async Task ReplaceReferenceEmbeddingsAsync(
        Tensor embeddings,
        VibeVoicePrompt prompt,
        IReadOnlyList<Pcm16WaveFile> references,
        int inferenceSteps,
        CancellationToken cancellationToken)
    {
        var acousticLatents = new List<Tensor>();
        var semanticLatents = new List<Tensor>();
        try
        {
            foreach (Pcm16WaveFile reference in references)
            {
                cancellationToken.ThrowIfCancellationRequested();
                using Tensor waveform = TensorFactory.FromArray(
                    reference.Samples24Khz
                        .Select(value => (BFloat16)value)
                        .ToArray(),
                    [1, 1, reference.Samples24Khz.Length]);
                acousticLatents.Add(
                    _components.Acoustic.Encode(waveform));
                semanticLatents.Add(
                    _components.Semantic.Encode(waveform));
            }
            var connected = new List<Tensor>();
            try
            {
                foreach ((Tensor acoustic, Tensor semantic) in
                         acousticLatents.Zip(semanticLatents))
                {
                    using Tensor scaled = ScaleSpeech(acoustic);
                    using Tensor acousticConnected =
                        _components.AcousticConnector.Forward(scaled);
                    using Tensor semanticConnected =
                        _components.SemanticConnector.Forward(semantic);
                    connected.Add(_components.Backend.Add(
                        acousticConnected,
                        semanticConnected));
                }
                using Tensor combined = _components.Backend.Concat(
                    connected,
                    1);
                InsertByMask(embeddings, prompt.SpeechInputMask, combined);
            }
            finally
            {
                foreach (Tensor tensor in connected)
                {
                    tensor.Dispose();
                }
            }
        }
        finally
        {
            foreach (Tensor tensor in acousticLatents)
            {
                tensor.Dispose();
            }
            foreach (Tensor tensor in semanticLatents)
            {
                tensor.Dispose();
            }
        }
        await Task.CompletedTask;
    }

    private void InsertByMask(
        Tensor embeddings,
        IReadOnlyList<bool> mask,
        Tensor replacement)
    {
        Span<BFloat16> target = embeddings.AsSpan<BFloat16>();
        ReadOnlySpan<BFloat16> source = replacement.AsSpan<BFloat16>();
        int width = checked((int)embeddings.Shape[2]);
        int outputRow = 0;
        for (int maskIndex = 0, sourceRow = 0;
             maskIndex < mask.Count && sourceRow < replacement.Shape[1];
             maskIndex++)
        {
            if (!mask[maskIndex])
            {
                continue;
            }
            source.Slice(sourceRow * width, width)
                .CopyTo(target.Slice(maskIndex * width, width));
            sourceRow++;
            outputRow++;
        }
        if (outputRow != replacement.Shape[1])
        {
            throw new InvalidOperationException(
                "Prompt speech mask does not match encoded reference length.");
        }
    }

    private Tensor ScaleSpeech(Tensor latent)
    {
        BFloat16[] source = latent.AsSpan<BFloat16>().ToArray();
        float scale = _components.SpeechScale;
        float bias = _components.SpeechBias;
        BFloat16[] values = source
            .Select(value => (BFloat16)(((float)value + bias) * scale))
            .ToArray();
        return TensorFactory.FromArray(values, latent.Shape.ToArray());
    }

    private Tensor InvertSpeechScaling(Tensor latent)
    {
        BFloat16[] source = latent.AsSpan<BFloat16>().ToArray();
        BFloat16[] values = source
            .Select(value => (BFloat16)(
                ((float)value / _components.SpeechScale) -
                _components.SpeechBias))
            .ToArray();
        return TensorFactory.FromArray(values, latent.Shape.ToArray());
    }

    private static Tensor LastHidden(Tensor hidden)
    {
        long width = hidden.Shape[^1];
        Tensor last = TensorFactory.Empty(
            hidden.DataType,
            1,
            width);
        int sourceOffset = checked((int)(hidden.ElementCount - width));
        if (hidden.DataType == DataType.BFloat16)
        {
            hidden.AsSpan<BFloat16>()
                .Slice(sourceOffset, checked((int)width))
                .CopyTo(last.AsSpan<BFloat16>());
        }

        else
        {
            last.Dispose();
            throw new NotSupportedException(
                "Generation logits require BF16 hidden states.");
        }
        return last;
    }

    private static Tensor LastSequenceValue(Tensor sequence)
    {
        if (sequence.Shape.Count != 3)
        {
            throw new ArgumentException(
                "Sequence extraction requires [batch, time, width].");
        }
        long width = sequence.Shape[2];
        long sequenceCount = sequence.Shape[1];
        Tensor output = TensorFactory.Empty(
            sequence.DataType,
            1,
            width);
        if (sequence.DataType == DataType.BFloat16)
        {
            sequence.AsSpan<BFloat16>()
                .Slice(
                    checked((int)((sequenceCount - 1) * width)),
                    checked((int)width))
                .CopyTo(output.AsSpan<BFloat16>());
        }
        else
        {
            output.Dispose();
            throw new NotSupportedException(
                "Semantic sequence extraction requires BF16.");
        }
        return output;
    }

    private static string PublishWave(string path, IReadOnlyList<float> samples)
    {
        string output = Path.GetFullPath(path);
        if (File.Exists(output))
        {
            throw new IOException($"Refusing to overwrite existing output: {output}");
        }
        Directory.CreateDirectory(
            Path.GetDirectoryName(output) ??
            throw new IOException("Output path has no directory."));
        string staging = output + "." + Guid.NewGuid().ToString("N") + ".stage";
        try
        {
            using (var stream = new FileStream(
                       staging,
                       FileMode.CreateNew,
                       FileAccess.Write,
                       FileShare.None,
                       64 * 1024))
            using (var writer = new BinaryWriter(stream))
            {
                writer.Write("RIFF".ToCharArray());
                writer.Write(36 + (samples.Count * 2));
                writer.Write("WAVE".ToCharArray());
                writer.Write("fmt ".ToCharArray());
                writer.Write(16);
                writer.Write((short)1);
                writer.Write((short)1);
                writer.Write(24000);
                writer.Write(24000 * 2);
                writer.Write((short)2);
                writer.Write((short)16);
                writer.Write("data".ToCharArray());
                writer.Write(samples.Count * 2);
                foreach (float sample in samples)
                {
                    float bounded = Math.Clamp(sample, -1.0f, 0.9999695f);
                    writer.Write((short)Math.Round(bounded * 32768));
                }
            }
            File.Move(staging, output);
            return output;
        }
        finally
        {
            if (File.Exists(staging))
            {
                File.Delete(staging);
            }
        }
    }

    private static async Task<string> Sha256Async(
        string path,
        CancellationToken cancellationToken)
    {
        await using var stream = File.OpenRead(path);
        byte[] hash = await SHA256.HashDataAsync(
            stream,
            cancellationToken);
        return Convert.ToHexStringLower(hash);
    }

    private static void ValidateOptions(VibeVoiceGenerationOptions options)
    {
        ArgumentNullException.ThrowIfNull(options);
        if (options.VoiceReferences.Count is < 1 or > 4 ||
            options.MaxNewTokens is < 1 or > 1024 ||
            options.MaxSeconds is <= 0 or > 60 ||
            options.InferenceSteps is < 1 or > 50 ||
            options.CfgScale is < 0 or > 4 ||
            options.TimeoutSeconds is <= 0 or > 3600)
        {
            throw new ArgumentException("Generation options exceed VibeVoice bounds.");
        }
        VibeVoiceSpeakerScript.Parse(options.Script);
    }

    public async ValueTask DisposeAsync()
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
        GC.Collect();
        await Task.Yield();
    }

    private sealed class CpuGenerationComponents
    {
        public CpuGenerationComponents(
            ITensorBackend backend,
            VibeVoiceConfiguration configuration,
            IReadOnlyDictionary<string, Tensor> state)
        {
            Backend = backend;
            Decoder = new(
                backend,
                configuration.Decoder,
                state);
            LanguageHead = state.TryGetValue(
                "lm_head.weight",
                out Tensor? languageHead)
                    ? languageHead
                    : throw new KeyNotFoundException("Language head state is missing.");
            Acoustic = new(
                backend,
                configuration.AcousticTokenizer,
                state);
            Semantic = new(
                backend,
                configuration.SemanticTokenizer,
                state);
            AcousticConnector = new(
                backend,
                state,
                "model.acoustic_connector.");
            SemanticConnector = new(
                backend,
                state,
                "model.semantic_connector.");
            Diffusion = new(
                backend,
                configuration.DiffusionHead,
                state);
            SpeechScale = state.TryGetValue(
                "model.speech_scaling_factor",
                out Tensor? speechScale)
                    ? ReadScalar(speechScale)
                    : throw new KeyNotFoundException(
                        "Speech scaling factor is missing.");
            SpeechBias = state.TryGetValue(
                "model.speech_bias_factor",
                out Tensor? speechBias)
                    ? ReadScalar(speechBias)
                    : throw new KeyNotFoundException("Speech bias factor is missing.");
        }

        public ITensorBackend Backend { get; }
        public Qwen2DecoderModel Decoder { get; }
        public Tensor LanguageHead { get; }
        public VibeVoiceAcousticTokenizer Acoustic { get; }
        public VibeVoiceSemanticTokenizer Semantic { get; }
        public VibeVoiceSpeechConnector AcousticConnector { get; }
        public VibeVoiceSpeechConnector SemanticConnector { get; }
        public VibeVoiceDiffusionHead Diffusion { get; }
        public float SpeechScale { get; }
        public float SpeechBias { get; }

        private static float ReadScalar(Tensor tensor) =>
            tensor.DataType == DataType.BFloat16
                ? tensor.AsSpan<BFloat16>()[0]
                : throw new InvalidDataException(
                    "Speech scaling state must be BF16.");
    }
}
