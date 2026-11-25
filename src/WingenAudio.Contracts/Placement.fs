namespace WingenAudio.Contracts

open System

type WorkerEngine =
    | VibeVoice
    | PersonaPlex

module WorkerDomain =
    [<CompiledName("ParseEngine")>]
    let parseEngine (value: string) =
        match value.Trim().ToLowerInvariant() with
        | "vibevoice" -> VibeVoice
        | "personaplex" -> PersonaPlex
        | _ -> invalidArg (nameof value) $"Unknown worker engine '{value}'."

    [<CompiledName("EngineId")>]
    let engineId =
        function
        | VibeVoice -> "vibevoice"
        | PersonaPlex -> "personaplex"

    [<CompiledName("EnginePort")>]
    let enginePort =
        function
        | VibeVoice -> 8996
        | PersonaPlex -> 8997

    let internal requiredWorkerGiB =
        function
        | VibeVoice -> 26.0
        | PersonaPlex -> 18.0

type WorkerAction =
    | Status
    | Preflight
    | Load
    | Unload
    | Generate

module WorkerActionDomain =
    [<CompiledName("ParseAction")>]
    let parseAction (value: string) =
        match value.Trim().ToLowerInvariant() with
        | "status" -> Status
        | "preflight" -> Preflight
        | "load" -> Load
        | "unload" -> Unload
        | "generate" -> Generate
        | _ -> invalidArg (nameof value) $"Unknown worker action '{value}'."

    [<CompiledName("MethodAndPath")>]
    let methodAndPath =
        function
        | Status -> "GET", "/v1/status"
        | Preflight -> "GET", "/v1/preflight"
        | Load -> "POST", "/v1/load"
        | Unload -> "POST", "/v1/unload"
        | Generate -> "POST", "/v1/generate"

type PlacementSignals =
    { Ok: bool
      OutputWritable: bool
      RootExists: bool
      ModelExists: bool
      Loaded: bool
      WorkerOnline: bool
      FreeMemoryGiB: double
      CpuCores: int
      GpuTotalMiB: double
      GpuFreeMiB: double
      GpuUtilizationPercent: double
      HasNativeHost: bool
      Weight: double }

module Placement =
    [<CompiledName("Score")>]
    let score engine signals =
        if
            not signals.Ok
            || not signals.OutputWritable
            || not signals.RootExists
            || not signals.ModelExists
            || signals.FreeMemoryGiB < WorkerDomain.requiredWorkerGiB engine
        then
            Double.NegativeInfinity
        else
            let mutable score =
                1.0
                + (if signals.Loaded then 20.0 else 0.0)
                + (if signals.WorkerOnline then 4.0 else 0.0)

            if signals.GpuTotalMiB > 0.0 then
                score <- score + min 20.0 (signals.GpuTotalMiB / 1024.0)
                score <- score + min 12.0 (signals.GpuFreeMiB / 1024.0)
                score <- score + max 0.0 ((100.0 - signals.GpuUtilizationPercent) / 20.0)
            else
                score <- score + double signals.CpuCores / 8.0

            score <- score + signals.FreeMemoryGiB / 16.0

            if signals.HasNativeHost then
                score <- score + 3.0

            score * signals.Weight
