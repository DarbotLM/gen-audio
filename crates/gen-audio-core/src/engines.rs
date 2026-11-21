//! Desktop engine availability. External engines can be used through the Python SDK.

use serde::Serialize;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum EngineKind {
    Synth,
    G2p,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum EngineStatus {
    Implemented,
    External,
    Library,
}

#[derive(Debug, Clone, Copy, Serialize)]
pub struct EngineSpec {
    pub id: &'static str,
    pub kind: EngineKind,
    pub status: EngineStatus,
    pub weights_bundled: bool,
    pub summary: &'static str,
}

const ENGINES: &[EngineSpec] = &[
    EngineSpec {
        id: "kokoro_onnx",
        kind: EngineKind::Synth,
        status: EngineStatus::Implemented,
        weights_bundled: false,
        summary: "Python kokoro-onnx path. Needs local ONNX and voices files. Weights are not in this repo.",
    },
    EngineSpec {
        id: "kokoro_dayour",
        kind: EngineKind::Synth,
        status: EngineStatus::External,
        weights_bundled: false,
        summary: "dayour/kokoro torch runtime. Not vendored here.",
    },
    EngineSpec {
        id: "misaki",
        kind: EngineKind::G2p,
        status: EngineStatus::Library,
        weights_bundled: false,
        summary: "dayour/misaki is G2P for Kokoro. It does not emit a waveform by itself.",
    },
    EngineSpec {
        id: "vibevoice",
        kind: EngineKind::Synth,
        status: EngineStatus::External,
        weights_bundled: false,
        summary: "Long-form VibeVoice with local model weights and a compatible inference runtime.",
    },
    EngineSpec {
        id: "vibevoice_realtime",
        kind: EngineKind::Synth,
        status: EngineStatus::Implemented,
        weights_bundled: false,
        summary: "VibeVoice Realtime 0.5B through the configured dlm_speech service.",
    },
    EngineSpec {
        id: "adaptive",
        kind: EngineKind::Synth,
        status: EngineStatus::Implemented,
        weights_bundled: false,
        summary: "Uses each cast speaker's engine and converts its output to a shared clock.",
    },
    EngineSpec {
        id: "magpie",
        kind: EngineKind::Synth,
        status: EngineStatus::External,
        weights_bundled: false,
        summary: "Magpie TTS with NVIDIA NeMo Speech and the NanoCodec decoder.",
    },
    EngineSpec {
        id: "pocket_tts",
        kind: EngineKind::Synth,
        status: EngineStatus::External,
        weights_bundled: false,
        summary: "Kyutai Pocket TTS with local CPU inference.",
    },
];

pub fn engines() -> &'static [EngineSpec] {
    ENGINES
}

pub fn engine(id: &str) -> Option<&'static EngineSpec> {
    ENGINES.iter().find(|spec| spec.id == id)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reports_implemented_adapters() {
        let implemented: Vec<_> = engines()
            .iter()
            .filter(|spec| spec.status == EngineStatus::Implemented)
            .map(|spec| spec.id)
            .collect();
        assert_eq!(implemented, vec!["kokoro_onnx", "vibevoice_realtime", "adaptive"]);
        assert_eq!(engine("misaki").unwrap().kind, EngineKind::G2p);
        assert!(engines().iter().all(|spec| !spec.weights_bundled));
    }
}
