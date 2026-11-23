//! Viewport tabs: Voice models, AgentVoice, Audio, Video, Cube. "Audio clips" is now
//! "Audio"; its slug stays slide:library and slide:audio is an alias.

use gen_audio_core::catalog::SLIDES;
use gen_audio_core::viewport::{resolve_slide, Viewport};

#[test]
fn agentvoice_slide_sits_between_voice_models_and_audio() {
    let index = |slug: &str| SLIDES.iter().position(|item| *item == slug).unwrap_or_else(|| panic!("{slug} missing"));
    assert!(index("models") < index("agentvoice"));
    assert!(index("agentvoice") < index("library"));
    assert!(index("library") < index("video"));
    assert!(index("video") < index("spatial"));
    assert_eq!(resolve_slide("slide:agentvoice").unwrap().canonical, "slide:agentvoice");
}

#[test]
fn audio_tab_is_titled_audio_and_keeps_the_library_slug() {
    let snap = Viewport::release().snapshot();
    let slides = snap["slides"].as_array().unwrap();
    let library = slides.iter().find(|slide| slide["id"] == "slide:library").expect("slide:library");
    assert_eq!(library["title"], "Audio");
    assert!(slides.iter().all(|slide| slide["title"] != "Audio clips" && slide["title"] != "Library"));
    let agentvoice = slides.iter().position(|slide| slide["id"] == "slide:agentvoice").expect("slide:agentvoice");
    let audio = slides.iter().position(|slide| slide["id"] == "slide:library").unwrap();
    assert!(agentvoice < audio);
    for alias in ["slide:audio", "slide:library"] {
        let resolved = resolve_slide(alias).unwrap();
        assert_eq!(resolved.canonical, "slide:library", "{alias}");
        assert!(!resolved.deprecated_alias, "{alias}");
    }
    assert!(resolve_slide("audio").unwrap().deprecated_alias);
}
