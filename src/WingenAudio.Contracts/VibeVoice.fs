namespace WingenAudio.Contracts

open System.Collections.Generic
open System.Text.Json.Serialization

type VibeVoiceGenerationRequest =
    { [<JsonPropertyName("schema_version")>]
      SchemaVersion: string
      [<JsonPropertyName("engine")>]
      Engine: string
      [<JsonPropertyName("model_dir")>]
      ModelDir: string
      [<JsonPropertyName("tokenizer")>]
      Tokenizer: string
      [<JsonPropertyName("scriptText")>]
      ScriptText: string
      [<JsonPropertyName("voiceRefs")>]
      VoiceRefs: string[]
      [<JsonPropertyName("output")>]
      Output: string
      [<JsonPropertyName("maxNewTokens")>]
      MaxNewTokens: int
      [<JsonPropertyName("maxSeconds")>]
      MaxSeconds: double
      [<JsonPropertyName("timeoutSeconds")>]
      TimeoutSeconds: double
      [<JsonPropertyName("seed")>]
      Seed: int64
      [<JsonPropertyName("cfgScale")>]
      CfgScale: double
      [<JsonPropertyName("inferenceSteps")>]
      InferenceSteps: int
      [<JsonPropertyName("execution")>]
      Execution: IDictionary<string, obj>
      [<JsonPropertyName("agentNames")>]
      AgentNames: string[] }

type VibeVoiceCapability =
    { Name: string
      Available: bool
      Boundary: string }

type VibeVoiceSpeakerLine =
    { Speaker: int
      Text: string }

type VibeVoiceTokenizedScript =
    { Speakers: VibeVoiceSpeakerLine[]
      InputIds: int[]
      SpeechInputMask: bool[]
      SpeechStartId: int
      SpeechEndId: int
      SpeechDiffusionId: int }

type VibeVoiceWaveInfo =
    { Path: string
      SampleRate: int
      TargetSampleRate: int
      Channels: int
      BitsPerSample: int
      DurationSeconds: double
      SourceSamples: int
      TargetSamples: int
      Peak: double }

type VibeVoiceSpecialTokens =
    { EndOfText: int
      SpeechStart: int
      SpeechEnd: int
      SpeechDiffusion: int }
