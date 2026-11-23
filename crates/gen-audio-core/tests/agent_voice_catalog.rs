//! Yumlog Studio AgentVoice catalog (apps/desktop/public/library/agentvoice/assets.json,
//! written by `python -m gen_audio.cli.agent_voices import`): every envelope is a valid
//! v1 asset whose uid re-mints in Rust, the set validates together with the Library
//! catalog, and every voice with audio carries its own spectrogram and cube.

use std::collections::BTreeMap;

use gen_audio_core::asset::{is_dev_fixture, validate_envelope, validate_set};
use serde_json::Value;

const AGENT_VOICES: &str = include_str!("../../../apps/desktop/public/library/agentvoice/assets.json");
const LIBRARY: &str = include_str!("../../../apps/desktop/public/library/assets.json");
const SCHEMA: &str = include_str!("../../../schemas/asset-object.schema.json");
const CARD_SCHEMA: &str = include_str!("../../../schemas/card-viewport.schema.json");
const PROFILE_SCHEMA: &str = include_str!("../../../schemas/voice_profile.schema.json");

fn json(text: &str) -> Value {
    serde_json::from_str(text).expect("json")
}

fn validator() -> jsonschema::Validator {
    let card = json(CARD_SCHEMA);
    let profile = json(PROFILE_SCHEMA);
    let registry = jsonschema::Registry::new()
        .add(card["$id"].as_str().unwrap().to_owned(), card)
        .expect("card schema registers")
        .add(profile["$id"].as_str().unwrap().to_owned(), profile)
        .expect("profile schema registers")
        .prepare()
        .expect("registry prepares");
    jsonschema::options()
        .with_draft(jsonschema::Draft::Draft202012)
        .with_registry(&registry)
        .build(&json(SCHEMA))
        .expect("asset-object.schema.json compiles")
}

#[test]
fn agent_voice_envelopes_are_valid_v1_and_re_mint() {
    let doc = json(AGENT_VOICES);
    let schema = validator();
    let assets = doc["assets"].as_array().expect("assets");
    assert!(!assets.is_empty(), "no AgentVoice assets imported");
    for asset in assets {
        let minted = validate_envelope(asset).unwrap_or_else(|e| panic!("{}: {e}", asset["uid"]));
        assert_eq!(minted.uid, asset["uid"].as_str().unwrap());
        assert_eq!(minted.glyph, asset["display"]["glyph"].as_str().unwrap());
        let errors: Vec<String> = schema.iter_errors(asset).map(|e| format!("{} at {}", e, e.instance_path())).collect();
        assert!(errors.is_empty(), "{} fails the JSON Schema: {errors:?}", asset["uid"]);
        assert!(!is_dev_fixture(asset), "{} is a dev fixture", asset["uid"]);
    }
}

#[test]
fn agent_voices_validate_with_the_library_catalog() {
    let mut all: Vec<Value> = json(LIBRARY)["assets"].as_array().unwrap().clone();
    all.extend(json(AGENT_VOICES)["assets"].as_array().unwrap().iter().cloned());
    validate_set(&all).unwrap_or_else(|e| panic!("library + AgentVoice set is invalid: {e}"));
}

#[test]
fn every_source_voice_has_a_row_and_every_voice_with_audio_has_spectrogram_and_cube() {
    let doc = json(AGENT_VOICES);
    let kinds: BTreeMap<&str, &str> =
        doc["assets"].as_array().unwrap().iter().map(|a| (a["uid"].as_str().unwrap(), a["kind"].as_str().unwrap())).collect();
    let voices = doc["voices"].as_array().unwrap();
    for source in doc["sources"].as_array().unwrap() {
        let rows = voices.iter().filter(|row| row["set"] == source["set"]).count() as u64;
        assert_eq!(Some(rows), source["voice_count"].as_u64(), "{}: rows vs source voices", source["set"]);
    }
    for row in voices {
        if row["status"] != "ok" {
            assert!(row["clip_uid"].is_null(), "{} is unavailable but names a clip", row["legacy_id"]);
            assert!(row["reason"].is_string(), "{} is unavailable with no reason", row["legacy_id"]);
            continue;
        }
        assert_eq!(kinds.get(row["clip_uid"].as_str().unwrap_or_default()), Some(&"audio_clip"));
        assert_eq!(kinds.get(row["spectrogram_uid"].as_str().unwrap_or_default()), Some(&"spectrogram_2d"), "{}", row["legacy_id"]);
        assert_eq!(kinds.get(row["cube_uid"].as_str().unwrap_or_default()), Some(&"cube_ihdr"), "{}", row["legacy_id"]);
    }
}
