using PowerTorch.Core;

namespace WingenAudio.VibeVoice;

public sealed class VibeVoiceSpeechConnector
{
    private readonly ITensorBackend _backend;
    private readonly Tensor _firstWeight;
    private readonly Tensor _firstBias;
    private readonly Tensor _norm;
    private readonly Tensor _secondWeight;
    private readonly Tensor _secondBias;

    public VibeVoiceSpeechConnector(
        ITensorBackend backend,
        IReadOnlyDictionary<string, Tensor> state,
        string prefix)
    {
        _backend = backend;
        _firstWeight = Require(state, prefix + "fc1.weight");
        _firstBias = Require(state, prefix + "fc1.bias");
        _norm = Require(state, prefix + "norm.weight");
        _secondWeight = Require(state, prefix + "fc2.weight");
        _secondBias = Require(state, prefix + "fc2.bias");
    }

    public Tensor Forward(Tensor input)
    {
        using Tensor first = TensorLinear.Forward(
            _backend,
            input,
            _firstWeight,
            _firstBias);
        using Tensor normalized = _backend.RmsNorm(
            first,
            [_norm.Shape[0]],
            _norm,
            1e-6);
        return TensorLinear.Forward(
            _backend,
            normalized,
            _secondWeight,
            _secondBias);
    }

    private static Tensor Require(
        IReadOnlyDictionary<string, Tensor> state,
        string name) =>
        state.TryGetValue(name, out Tensor? tensor)
            ? tensor
            : throw new KeyNotFoundException($"Connector state is missing '{name}'.");
}
