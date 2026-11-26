using System.Text.Json;

namespace WingenAudio.VibeVoice;

public sealed record VibeVoiceConfiguration(
    int AcousticVaeDimension,
    int SemanticVaeDimension,
    Qwen2DecoderConfiguration Decoder,
    AcousticTokenizerConfiguration AcousticTokenizer,
    AcousticTokenizerConfiguration SemanticTokenizer,
    DiffusionHeadConfiguration DiffusionHead,
    ProcessorConfiguration Processor)
{
    public static VibeVoiceConfiguration Load(string modelDirectory)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(modelDirectory);
        string root = Path.GetFullPath(modelDirectory);
        using JsonDocument configDocument = JsonDocument.Parse(
            File.ReadAllBytes(Path.Combine(root, "config.json")));
        using JsonDocument processorDocument = JsonDocument.Parse(
            File.ReadAllBytes(Path.Combine(root, "preprocessor_config.json")));

        JsonElement config = configDocument.RootElement;
        JsonElement decoder = Required(config, "decoder_config");
        JsonElement acoustic = Required(config, "acoustic_tokenizer_config");
        JsonElement semantic = Required(config, "semantic_tokenizer_config");
        JsonElement diffusion = Required(config, "diffusion_head_config");
        JsonElement processor = processorDocument.RootElement;

        if (String(config, "model_type") != "vibevoice" ||
            String(decoder, "model_type") != "qwen2")
        {
            throw new InvalidDataException("Expected a VibeVoice model with a Qwen2 decoder.");
        }

        var result = new VibeVoiceConfiguration(
            Integer(config, "acostic_vae_dim"),
            Integer(config, "semantic_vae_dim"),
            new Qwen2DecoderConfiguration(
                Integer(decoder, "hidden_size"),
                Integer(decoder, "intermediate_size"),
                Integer(decoder, "num_hidden_layers"),
                Integer(decoder, "num_attention_heads"),
                Integer(decoder, "num_key_value_heads"),
                Integer(decoder, "vocab_size"),
                Integer(decoder, "max_position_embeddings"),
                Double(decoder, "rms_norm_eps"),
                Double(decoder, "rope_theta"),
                String(decoder, "hidden_act"),
                Boolean(config, "tie_word_embeddings")),
            ParseTokenizer(acoustic),
            ParseTokenizer(semantic),
            new DiffusionHeadConfiguration(
                Integer(diffusion, "hidden_size"),
                Integer(diffusion, "latent_size"),
                Integer(diffusion, "head_layers"),
                Double(diffusion, "head_ffn_ratio"),
                Integer(diffusion, "ddpm_num_steps"),
                Integer(diffusion, "ddpm_num_inference_steps"),
                String(diffusion, "ddpm_beta_schedule"),
                String(diffusion, "prediction_type"),
                Double(diffusion, "rms_norm_eps")),
            new ProcessorConfiguration(
                Integer(processor, "speech_tok_compress_ratio"),
                Integer(Required(processor, "audio_processor"), "sampling_rate"),
                Double(Required(processor, "audio_processor"), "target_dB_FS")));
        result.Validate();
        return result;
    }

    public void Validate()
    {
        if (Decoder.HiddenSize % Decoder.AttentionHeads != 0)
        {
            throw new InvalidDataException("Qwen2 hidden size is not divisible by its head count.");
        }
        if (Decoder.AttentionHeads % Decoder.KeyValueHeads != 0)
        {
            throw new InvalidDataException("Qwen2 query heads are not divisible by key/value heads.");
        }
        if (!string.Equals(Decoder.Activation, "silu", StringComparison.OrdinalIgnoreCase) ||
            AcousticVaeDimension != DiffusionHead.LatentSize ||
            Processor.SampleRate != 24000 ||
            Processor.CompressionRatio != 3200)
        {
            throw new InvalidDataException("Unsupported VibeVoice architecture parameters.");
        }
    }

    private static AcousticTokenizerConfiguration ParseTokenizer(JsonElement value) =>
        new(
            String(value, "model_type"),
            Integer(value, "channels"),
            Integer(value, "vae_dim"),
            Integer(value, "encoder_n_filters"),
            Integers(value, "encoder_depths"),
            ParseRatios(Required(value, "encoder_ratios")),
            ParseRatios(Optional(value, "decoder_ratios")),
            Boolean(value, "causal"),
            Double(value, "layernorm_eps"),
            value.TryGetProperty("decoder_n_filters", out JsonElement decoderFilters)
                ? decoderFilters.GetInt32()
                : Integer(value, "encoder_n_filters"),
            value.TryGetProperty("decoder_depths", out JsonElement decoderDepths) &&
                decoderDepths.ValueKind is not JsonValueKind.Null
                    ? Integers(value, "decoder_depths")
                    : Integers(value, "encoder_depths").Reverse().ToArray(),
            Double(value, "fix_std"),
            String(value, "std_dist_type"));

    private static int[] ParseRatios(JsonElement value)
    {
        if (value.ValueKind is JsonValueKind.Null or JsonValueKind.Undefined)
        {
            return [];
        }

        return value.EnumerateArray().Select(item => item.GetInt32()).ToArray();
    }

    private static int[] Integers(JsonElement parent, string name)
    {
        JsonElement value = Required(parent, name);
        return value.ValueKind == JsonValueKind.Array
            ? value.EnumerateArray().Select(item => item.GetInt32()).ToArray()
            : (value.GetString() ?? string.Empty)
                .Split('-', StringSplitOptions.RemoveEmptyEntries)
                .Select(int.Parse)
                .ToArray();
    }

    private static JsonElement Required(JsonElement parent, string name) =>
        parent.TryGetProperty(name, out JsonElement value)
            ? value
            : throw new InvalidDataException($"VibeVoice configuration is missing '{name}'.");

    private static JsonElement Optional(JsonElement parent, string name) =>
        parent.TryGetProperty(name, out JsonElement value) ? value : default;

    private static string String(JsonElement parent, string name) =>
        Required(parent, name).GetString()
        ?? throw new InvalidDataException($"VibeVoice configuration field '{name}' must be a string.");

    private static int Integer(JsonElement parent, string name) =>
        Required(parent, name).GetInt32();

    private static bool Boolean(JsonElement parent, string name) =>
        Required(parent, name).GetBoolean();

    private static double Double(JsonElement parent, string name) =>
        Required(parent, name).GetDouble();
}

public sealed record Qwen2DecoderConfiguration(
    int HiddenSize,
    int IntermediateSize,
    int Layers,
    int AttentionHeads,
    int KeyValueHeads,
    int VocabularySize,
    int MaxPositionEmbeddings,
    double RmsNormEpsilon,
    double RopeTheta,
    string Activation,
    bool TieWordEmbeddings);

public sealed record AcousticTokenizerConfiguration(
    string ModelType,
    int Channels,
    int VaeDimension,
    int Filters,
    int[] EncoderDepths,
    int[] EncoderRatios,
    int[] DecoderRatios,
    bool Causal,
    double NormEpsilon,
    int DecoderFilters,
    int[] DecoderDepths,
    double FixedStandardDeviation,
    string DistributionType);

public sealed record DiffusionHeadConfiguration(
    int HiddenSize,
    int LatentSize,
    int Layers,
    double FfnRatio,
    int TrainingSteps,
    int InferenceSteps,
    string BetaSchedule,
    string PredictionType,
    double RmsNormEpsilon);

public sealed record ProcessorConfiguration(
    int CompressionRatio,
    int SampleRate,
    double TargetDbFullScale);
