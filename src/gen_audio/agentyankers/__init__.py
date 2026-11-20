"""AgentYankers format for the public GenAID Audio SDK.

Live calls stay in ``genaid_audio.agentyankers`` when that package is installed.
This package mixes a rundown: VibeVoice commentary, laugh and stinger cues, a
bed, a spectrogram, and a cube revision.
"""

from gen_audio.agentyankers.contract import (
    DOCUMENTED_VOICE_ROOT,
    ENV_LIVEKIT_API_KEY,
    ENV_LIVEKIT_API_SECRET,
    ENV_LIVEKIT_URL,
    ENV_PERSONAPLEX_MODEL,
    ENV_VIBEVOICE_MODEL,
    ENV_VOICES_DIR,
    PRODUCER,
    RECORDING_DISCLOSURE,
    SAMPLE_RATE,
    SCHEMA_VERSION,
    SHOW_NAME,
    AgentYankersError,
    ConsentRefusal,
    CoreUnavailable,
    CredentialsMissing,
    MissingRecording,
    PhoneNumberRefusal,
    VibeVoiceRuntimeUnavailable,
    assert_no_phone_numbers,
    explain_consent,
    recording_disclosure,
    reject_inline_secrets,
    require_call_consent,
    require_publish_consent,
)
from gen_audio.agentyankers.core import assemble_with_core, core_status, import_core
from gen_audio.agentyankers.episode import render_episode
from gen_audio.agentyankers.schema import export_json_schemas
from gen_audio.agentyankers.voiceover import GenAudioVoiceoverRenderer, commentary_script

__all__ = [
    "SCHEMA_VERSION",
    "SHOW_NAME",
    "PRODUCER",
    "SAMPLE_RATE",
    "RECORDING_DISCLOSURE",
    "DOCUMENTED_VOICE_ROOT",
    "ENV_VOICES_DIR",
    "ENV_LIVEKIT_URL",
    "ENV_LIVEKIT_API_KEY",
    "ENV_LIVEKIT_API_SECRET",
    "ENV_PERSONAPLEX_MODEL",
    "ENV_VIBEVOICE_MODEL",
    "AgentYankersError",
    "ConsentRefusal",
    "PhoneNumberRefusal",
    "CredentialsMissing",
    "CoreUnavailable",
    "VibeVoiceRuntimeUnavailable",
    "MissingRecording",
    "require_call_consent",
    "require_publish_consent",
    "recording_disclosure",
    "assert_no_phone_numbers",
    "reject_inline_secrets",
    "explain_consent",
    "import_core",
    "core_status",
    "assemble_with_core",
    "export_json_schemas",
    "GenAudioVoiceoverRenderer",
    "commentary_script",
    "render_episode",
]
