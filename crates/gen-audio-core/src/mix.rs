//! Offline mix-project validation. Reuses ga1 catalog checks and canonical
//! integer JSON; no media reads, rendering, jobs, or asset publication.

use std::collections::BTreeMap;
use std::sync::OnceLock;

use serde_json::{json, Value};

use crate::asset;
use crate::asset_catalog::{self, CatalogError, Resolve};

const MAX_PROJECT_BYTES: usize = 64 * 1024;
const MAX_LAYERS: usize = 16;
const SCHEMA: &str = include_str!("../../../schemas/mix-project.schema.json");

fn schema() -> &'static Value {
    static SCHEMA_VALUE: OnceLock<Value> = OnceLock::new();
    SCHEMA_VALUE.get_or_init(|| serde_json::from_str(SCHEMA).expect("compiled mix schema"))
}

fn validator() -> &'static jsonschema::Validator {
    static VALIDATOR: OnceLock<jsonschema::Validator> = OnceLock::new();
    VALIDATOR.get_or_init(|| {
        jsonschema::options()
            .with_draft(jsonschema::Draft::Draft202012)
            .build(schema())
            .expect("compiled mix schema validates")
    })
}

/// Inline local definitions for MCP clients without external schema retrieval.
pub fn project_schema() -> Value {
    fn expand(value: &Value) -> Value {
        match value {
            Value::Object(map) => {
                let mut expanded = match map.get("$ref").and_then(Value::as_str) {
                    Some(reference) => {
                        expand(&schema().pointer(&reference[1..]).expect("local definition"))
                    }
                    None => json!({}),
                };
                for (key, child) in map {
                    if !matches!(key.as_str(), "$ref" | "$defs" | "$id" | "$schema") {
                        expanded[key] = expand(child);
                    }
                }
                expanded
            }
            Value::Array(items) => Value::Array(items.iter().map(expand).collect()),
            _ => value.clone(),
        }
    }
    expand(schema())
}

fn defaults(value: &mut Value, definition: &Value) {
    for (key, property) in definition["properties"]
        .as_object()
        .expect("object definition")
    {
        let default = property.get("default").or_else(|| {
            let reference = property.get("$ref")?.as_str()?;
            schema().pointer(&reference[1..])?.get("default")
        });
        if value.get(key).is_none() {
            if let Some(default) = default {
                value[key] = default.clone();
            }
        }
    }
}

fn invalid(message: impl Into<String>) -> (i32, String) {
    (-32602, message.into())
}

fn refused(code: &str, reason: &str) -> Value {
    json!({
        "ok": false, "code": code, "reason": reason,
        "renderedAudio": false, "synthesizedSpeech": false,
        "absolutePathsOmitted": true
    })
}

fn visit(index: usize, edges: &[Vec<usize>], state: &mut [u8]) -> Result<(), (i32, String)> {
    match state[index] {
        1 => return Err(invalid("Duck keys form a cycle")),
        2 => return Ok(()),
        _ => {}
    }
    state[index] = 1;
    for &key in &edges[index] {
        visit(key, edges, state)?;
    }
    state[index] = 2;
    Ok(())
}

/// Validate the complete project before resolving metadata. A successful result
/// is a normalized plan, never evidence that source media exists or was rendered.
pub fn validate(project: &Value) -> Result<Value, (i32, String)> {
    let bytes = serde_json::to_vec(project).map_err(|_| invalid("Invalid project JSON"))?;
    if bytes.len() > MAX_PROJECT_BYTES {
        let mut result = refused("limit_exceeded", "Mix project exceeds 64 KiB");
        result["limit"] =
            json!({"name": "projectBytes", "max": MAX_PROJECT_BYTES, "got": bytes.len()});
        return Ok(result);
    }
    if let Some(layers) = project["layers"].as_array() {
        if layers.len() > MAX_LAYERS {
            let mut result = refused("limit_exceeded", "Mix project exceeds 16 layers");
            result["limit"] = json!({"name": "layers", "max": MAX_LAYERS, "got": layers.len()});
            return Ok(result);
        }
    }
    if let Some(error) = validator().iter_errors(project).next() {
        // Do not echo source values or arbitrary unknown keys.
        return Err(invalid(format!(
            "Invalid mix project ({})",
            error.schema_path()
        )));
    }

    let mut normalized = project.clone();
    defaults(&mut normalized, schema());
    defaults(&mut normalized["master"], &schema()["$defs"]["master"]);
    let layers = normalized["layers"]
        .as_array_mut()
        .expect("validated layers");
    let mut ids = BTreeMap::new();
    for (index, layer) in layers.iter_mut().enumerate() {
        defaults(layer, &schema()["$defs"]["layer"]);
        if layer.get("solo_safe").is_none() {
            layer["solo_safe"] = json!(layer["role"] == "voice");
        }
        if layer["duck"].is_null() {
            layer
                .as_object_mut()
                .expect("validated layer")
                .remove("duck");
        }
        if layer["loop"]["mode"] != "off" && layer["loop"].get("xfade_ms").is_none() {
            layer["loop"]["xfade_ms"] = json!(500);
        }
        let id = layer["id"].as_str().expect("validated id").to_owned();
        if ids.insert(id, index).is_some() {
            return Err(invalid("Layer ids must be unique"));
        }
    }
    let mut edges = vec![Vec::new(); layers.len()];
    for (index, layer) in layers.iter().enumerate() {
        if let Some(keys) = layer["duck"]["keys"].as_array() {
            for key in keys {
                let target = ids
                    .get(key.as_str().expect("validated duck key"))
                    .ok_or_else(|| invalid("Duck key does not name a layer"))?;
                if *target == index {
                    return Err(invalid("A layer cannot duck itself"));
                }
                edges[index].push(*target);
            }
        }
    }
    let mut state = vec![0; layers.len()];
    for index in 0..layers.len() {
        visit(index, &edges, &mut state)?;
    }
    // JSON Schema accepts integral floats; ga1 identity requires integer tokens.
    let canonical = asset::canonicalize(&normalized)
        .map_err(|_| invalid("Mix project requires canonical integer JSON"))?;

    asset_catalog::catalog_health().map_err(|_| (-32603, "Asset catalog is invalid".into()))?;
    let mut warnings = vec![json!(
        "Metadata validation only; source media bytes and audio rendering were not checked."
    )];
    let mut sources = BTreeMap::new();
    for layer in normalized["layers"].as_array().expect("validated layers") {
        let uid = layer["source"].as_str().expect("validated source");
        let kind = uid.split(':').nth(1).expect("validated uid");
        if matches!(kind, "music_track" | "ambient_bed" | "mix") {
            return Ok(refused(
                "not_configured",
                "Music and nested mix source assets are not implemented in this build",
            ));
        }
        let (kind, _) = asset::parse_uid(uid).map_err(|_| invalid("Invalid source uid"))?;
        if kind != "audio_clip" {
            return Ok(refused(
                "kind_mismatch",
                "Mix sources must be audio_clip, music_track, ambient_bed, or mix",
            ));
        }
        if !sources.contains_key(uid) {
            let source = match asset_catalog::resolve(uid).map_err(CatalogError::rpc)? {
                Resolve::Found(source) => source,
                Resolve::NotFound => {
                    return Ok(refused(
                        "asset_not_found",
                        "A source uid is not in the release or user library",
                    ))
                }
                Resolve::Ambiguous(_) => {
                    return Err((-32603, "Full source uid resolved ambiguously".into()))
                }
            };
            sources.insert(uid.to_owned(), source);
        }
        let source = &sources[uid];
        let duration = source["fields"]["duration_ms"]
            .as_u64()
            .ok_or_else(|| (-32603, "Source duration is invalid".into()))?;
        if source["fields"]["channels"].as_u64().unwrap_or(0) > 2 {
            return Ok(refused(
                "unsupported_format",
                "Mix planning supports only mono or stereo source metadata",
            ));
        }
        let trim = layer["trim_start_ms"].as_u64().expect("validated trim")
            + layer["trim_end_ms"].as_u64().expect("validated trim");
        if trim >= duration {
            return Err(invalid("Layer trims must leave a positive source duration"));
        }
        if layer["loop"]["xfade_ms"].as_u64().unwrap_or(0) > duration - trim {
            return Err(invalid(
                "Loop crossfade exceeds the trimmed source duration",
            ));
        }
        if source["status"] != "ok" {
            warnings.push(json!(format!(
                "Layer {} has unavailable source media metadata.",
                layer["id"].as_str().unwrap()
            )));
        }
        if layer["offset_ms"].as_u64().expect("validated offset")
            >= normalized["duration_ms"]
                .as_u64()
                .expect("validated duration")
        {
            warnings.push(json!(format!(
                "Layer {} starts at or after the project end.",
                layer["id"].as_str().unwrap()
            )));
        }
    }
    Ok(json!({
        "ok": true,
        "normalized": normalized,
        "projectSha256": asset::sha256_hex(canonical.as_bytes()),
        "warnings": warnings,
        "renderedAudio": false,
        "synthesizedSpeech": false,
        "absolutePathsOmitted": true
    }))
}
