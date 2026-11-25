namespace WingenAudio.VibeVoice;

internal static class VibeVoiceDiffusionState
{
    public static void Validate(
        VibeVoiceStateMap state,
        ICollection<string> errors)
    {
        DiffusionHeadConfiguration configuration =
            state.Configuration.DiffusionHead;
        int hidden = configuration.HiddenSize;
        int latent = configuration.LatentSize;
        int ffn = checked((int)Math.Round(hidden * configuration.FfnRatio));
        state.RequireDecoderShape(
            "model.prediction_head.noisy_images_proj.weight",
            [hidden, latent],
            errors);
        state.RequireDecoderShape(
            "model.prediction_head.cond_proj.weight",
            [hidden, hidden],
            errors);
        state.RequireDecoderShape(
            "model.prediction_head.t_embedder.mlp.0.weight",
            [hidden, 256],
            errors);
        state.RequireDecoderShape(
            "model.prediction_head.t_embedder.mlp.2.weight",
            [hidden, hidden],
            errors);
        for (int layer = 0; layer < configuration.Layers; layer++)
        {
            string prefix = $"model.prediction_head.layers.{layer}.";
            state.RequireDecoderShape(prefix + "norm.weight", [hidden], errors);
            state.RequireDecoderShape(
                prefix + "adaLN_modulation.1.weight",
                [hidden * 3, hidden],
                errors);
            state.RequireDecoderShape(
                prefix + "ffn.gate_proj.weight",
                [ffn, hidden],
                errors);
            state.RequireDecoderShape(
                prefix + "ffn.up_proj.weight",
                [ffn, hidden],
                errors);
            state.RequireDecoderShape(
                prefix + "ffn.down_proj.weight",
                [hidden, ffn],
                errors);
        }
        state.RequireDecoderShape(
            "model.prediction_head.final_layer.adaLN_modulation.1.weight",
            [hidden * 2, hidden],
            errors);
        state.RequireDecoderShape(
            "model.prediction_head.final_layer.linear.weight",
            [latent, hidden],
            errors);
    }
}
