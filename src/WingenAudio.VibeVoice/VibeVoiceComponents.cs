using PowerTorch.Core;

namespace WingenAudio.VibeVoice;

public interface IAcousticTokenizer
{
    int LatentDimension { get; }

    bool CanEncode { get; }

    bool CanDecode { get; }

    Tensor Encode(Tensor waveform);

    Tensor Decode(Tensor latent);
}

public interface ISemanticTokenizer
{
    int LatentDimension { get; }

    bool CanEncode { get; }

    Tensor Encode(Tensor waveform);
}

public interface IDiffusionHead
{
    int LatentDimension { get; }

    int InferenceSteps { get; }

    Tensor Predict(Tensor noisyLatent, Tensor timestep, Tensor condition);
}

public sealed record VibeVoiceGraphPlan(
    Qwen2DecoderConfiguration Decoder,
    string AcousticTokenizer,
    string SemanticTokenizer,
    DiffusionHeadPlan DiffusionHead,
    GenerationPlan Generation);

public sealed record DiffusionHeadPlan(
    int LatentSize,
    int HiddenSize,
    int Layers,
    int FfnHiddenSize,
    int TrainingSteps,
    int InferenceSteps,
    string Scheduler,
    string PredictionType,
    IReadOnlyList<string> StatePrefixes);

public sealed record GenerationPlan(
    int SampleRate,
    int SpeechCompressionRatio,
    double DefaultCfgScale,
    IReadOnlyList<string> Stages,
    string Refusal);

public static class VibeVoiceGraph
{
    public static VibeVoiceGraphPlan Build(
        VibeVoiceConfiguration configuration,
        VibeVoiceStateMap state)
    {
        ArgumentNullException.ThrowIfNull(configuration);
        ArgumentNullException.ThrowIfNull(state);
        state.Validate();

        return new(
            configuration.Decoder,
            configuration.AcousticTokenizer.ModelType,
            configuration.SemanticTokenizer.ModelType,
            new DiffusionHeadPlan(
                configuration.DiffusionHead.LatentSize,
                configuration.DiffusionHead.HiddenSize,
                configuration.DiffusionHead.Layers,
                checked((int)Math.Round(
                    configuration.DiffusionHead.HiddenSize *
                    configuration.DiffusionHead.FfnRatio)),
                configuration.DiffusionHead.TrainingSteps,
                configuration.DiffusionHead.InferenceSteps,
                configuration.DiffusionHead.BetaSchedule,
                configuration.DiffusionHead.PredictionType,
                [
                    "model.prediction_head.noisy_images_proj.weight",
                    "model.prediction_head.cond_proj.weight",
                    "model.prediction_head.t_embedder.mlp.0.weight",
                    "model.prediction_head.t_embedder.mlp.2.weight",
                    "model.prediction_head.layers.",
                    "model.prediction_head.final_layer.",
                ]),
            new GenerationPlan(
                configuration.Processor.SampleRate,
                configuration.Processor.CompressionRatio,
                1.3,
                [
                    "tokenize-script-and-reference-audio",
                    "encode-reference-waveforms-with-acoustic-tokenizer",
                    "connect-acoustic-and-semantic-features-to-Qwen2",
                    "prefill-Qwen2-and-advance-generation-state",
                    "diffusion-sample-acoustic-latents-with-classifier-free-guidance",
                    "decode-latents-with-acoustic-tokenizer",
                    "write-24kHz-PCM-WAV",
                ],
                "Generation is refused until the PowerTorch execution primitives below are available; no audio is synthesized."));
    }
}
