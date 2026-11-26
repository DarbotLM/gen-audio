namespace WingenAudio.VibeVoice;

internal static class VibeVoiceTokenizerState
{
    public static void Validate(
        VibeVoiceStateMap state,
        ICollection<string> errors)
    {
        ValidateEncoder(
            state,
            state.Configuration.AcousticTokenizer,
            "model.acoustic_tokenizer.encoder.",
            errors);
        ValidateDecoder(
            state,
            state.Configuration.AcousticTokenizer,
            "model.acoustic_tokenizer.decoder.",
            errors);
        ValidateEncoder(
            state,
            state.Configuration.SemanticTokenizer,
            "model.semantic_tokenizer.encoder.",
            errors);
    }

    private static void ValidateEncoder(
        VibeVoiceStateMap state,
        AcousticTokenizerConfiguration configuration,
        string prefix,
        ICollection<string> errors)
    {
        int[] depths = configuration.EncoderDepths;
        int[] ratios = configuration.EncoderRatios.Reverse().ToArray();
        if (depths.Length != ratios.Length + 1)
        {
            errors.Add($"Encoder '{prefix}' ratios and depths do not define one stem plus ratios.");
            return;
        }
        Require(
            state,
            prefix + "downsample_layers.0.0.conv.conv.weight",
            [configuration.Filters, configuration.Channels, 7],
            errors);
        Require(
            state,
            prefix + "downsample_layers.0.0.conv.conv.bias",
            [configuration.Filters],
            errors);
        for (int stage = 1; stage < depths.Length; stage++)
        {
            int input = configuration.Filters * (1 << (stage - 1));
            int output = input * 2;
            Require(
                state,
                prefix + $"downsample_layers.{stage}.0.conv.conv.weight",
                [output, input, ratios[stage - 1] * 2],
                errors);
            Require(
                state,
                prefix + $"downsample_layers.{stage}.0.conv.conv.bias",
                [output],
                errors);
        }
        for (int stage = 0; stage < depths.Length; stage++)
        {
            ValidateStages(
                state,
                prefix,
                stage,
                depths[stage],
                configuration.Filters * (1 << stage),
                errors);
        }
        Require(
            state,
            prefix + "head.conv.conv.weight",
            [configuration.VaeDimension, configuration.Filters * (1 << (depths.Length - 1)), 7],
            errors);
        Require(
            state,
            prefix + "head.conv.conv.bias",
            [configuration.VaeDimension],
            errors);
    }

    private static void ValidateDecoder(
        VibeVoiceStateMap state,
        AcousticTokenizerConfiguration configuration,
        string prefix,
        ICollection<string> errors)
    {
        int[] depths = configuration.DecoderDepths;
        int lastStage = depths.Length - 1;
        if (depths.Length != configuration.DecoderRatios.Length + 1)
        {
            errors.Add($"Decoder '{prefix}' ratios and depths do not define one stem plus ratios.");
            return;
        }
        int largest = configuration.DecoderFilters * (1 << lastStage);
        Require(
            state,
            prefix + "upsample_layers.0.0.conv.conv.weight",
            [largest, configuration.VaeDimension, 7],
            errors);
        Require(
            state,
            prefix + "upsample_layers.0.0.conv.conv.bias",
            [largest],
            errors);
        for (int index = 0; index < configuration.DecoderRatios.Length; index++)
        {
            int input = configuration.DecoderFilters * (1 << (lastStage - index));
            int output = input / 2;
            Require(
                state,
                prefix + $"upsample_layers.{index + 1}.0.convtr.convtr.weight",
                [input, output, configuration.DecoderRatios[index] * 2],
                errors);
            Require(
                state,
                prefix + $"upsample_layers.{index + 1}.0.convtr.convtr.bias",
                [output],
                errors);
        }
        for (int stage = 0; stage < depths.Length; stage++)
        {
            ValidateStages(
                state,
                prefix,
                stage,
                depths[stage],
                configuration.DecoderFilters * (1 << (lastStage - stage)),
                errors);
        }
        Require(
            state,
            prefix + "head.conv.conv.weight",
            [configuration.Channels, configuration.DecoderFilters, 7],
            errors);
        Require(
            state,
            prefix + "head.conv.conv.bias",
            [configuration.Channels],
            errors);
    }

    private static void ValidateStages(
        VibeVoiceStateMap state,
        string prefix,
        int stage,
        int count,
        int channels,
        ICollection<string> errors)
    {
        for (int block = 0; block < count; block++)
        {
            string blockPrefix = prefix + $"stages.{stage}.{block}.";
            Require(state, blockPrefix + "norm.weight", [channels], errors);
            Require(state, blockPrefix + "gamma", [channels], errors);
            Require(state, blockPrefix + "ffn_norm.weight", [channels], errors);
            Require(state, blockPrefix + "ffn_gamma", [channels], errors);
            Require(
                state,
                blockPrefix + "mixer.conv.conv.conv.weight",
                [channels, 1, 7],
                errors);
            Require(
                state,
                blockPrefix + "mixer.conv.conv.conv.bias",
                [channels],
                errors);
            Require(
                state,
                blockPrefix + "ffn.linear1.weight",
                [channels * 4, channels],
                errors);
            Require(
                state,
                blockPrefix + "ffn.linear1.bias",
                [channels * 4],
                errors);
            Require(
                state,
                blockPrefix + "ffn.linear2.weight",
                [channels, channels * 4],
                errors);
            Require(
                state,
                blockPrefix + "ffn.linear2.bias",
                [channels],
                errors);
        }
    }

    private static void Require(
        VibeVoiceStateMap state,
        string name,
        IReadOnlyList<long> expected,
        ICollection<string> errors) =>
        state.RequireDecoderShape(name, expected, errors);
}
