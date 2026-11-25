namespace WingenAudio.Contracts

open System
open System.Collections.Generic
open System.IO
open System.IO.Compression
open System.Numerics
open System.Security.Cryptography
open System.Text
open System.Text.Json.Nodes

type CubePoint =
    { X: int
      Y: int
      Z: float
      Value: float
      Layer: int }

type CubeLayerResult =
    { OutputPath: string
      PngPath: string option
      SourceSha256: string
      PointCount: int
      CubeShape: int * int
      DurationSeconds: float
      InvHdr: float }

module CubeLayers =
    let private revision = 3
    let private layerMethod = "library_r3"
    let private generatorPath = "src/WingenAudio.Contracts/CubeLayers.fs"
    let private layerNames = [| "signal"; "tonality"; "confidence"; "quality" |]
    let private layerColors = [| 0.15, 0.55, 1.0; 0.2, 0.9, 0.35; 1.0, 0.75, 0.1; 0.95, 0.25, 0.55 |]
    let private nFft = 1024
    let private hop = 256
    let private maxF = 128
    let private maxT = 400
    let private threshold = 0.12
    let private perLayer = 900
    let private epsilon = 1e-12

    let private require condition message =
        if not condition then invalidArg "value" message

    let private normalizeLineEndings (bytes: byte[]) =
        let mutable count = 0

        for i = 0 to bytes.Length - 1 do
            if not (i > 0 && bytes[i - 1] = byte '\r' && bytes[i] = byte '\n') then
                count <- count + 1

        let normalized = Array.zeroCreate<byte> count
        let mutable target = 0

        for i = 0 to bytes.Length - 1 do
            if not (i > 0 && bytes[i - 1] = byte '\r' && bytes[i] = byte '\n') then
                normalized[target] <- bytes[i]
                target <- target + 1

        normalized

    let private sha256Bytes (bytes: byte[]) =
        Convert.ToHexString(SHA256.HashData(bytes)).ToLowerInvariant()

    let private sourceHash path = sha256Bytes (File.ReadAllBytes path)

    let private generatorHash repoRoot =
        Path.Combine(repoRoot, generatorPath.Replace('/', Path.DirectorySeparatorChar))
        |> File.ReadAllBytes
        |> normalizeLineEndings
        |> sha256Bytes

    let private readWav path =
        use stream = File.OpenRead path
        use reader = new BinaryReader(stream, Encoding.ASCII, leaveOpen = false)
        require (Encoding.ASCII.GetString(reader.ReadBytes 4) = "RIFF") "WAV is missing a RIFF header."
        reader.ReadUInt32() |> ignore
        require (Encoding.ASCII.GetString(reader.ReadBytes 4) = "WAVE") "WAV is missing a WAVE header."

        let mutable audioFormat = 0us
        let mutable channels = 0us
        let mutable sampleRate = 0u
        let mutable bits = 0us
        let mutable pcm: byte[] option = None

        while stream.Position + 8L <= stream.Length do
            let id = Encoding.ASCII.GetString(reader.ReadBytes 4)
            let size = int64 (reader.ReadUInt32())
            let start = stream.Position

            match id with
            | "fmt " when size >= 16L ->
                audioFormat <- reader.ReadUInt16()
                channels <- reader.ReadUInt16()
                sampleRate <- reader.ReadUInt32()
                reader.ReadUInt32() |> ignore
                reader.ReadUInt16() |> ignore
                bits <- reader.ReadUInt16()
            | "data" -> pcm <- Some(reader.ReadBytes(int size))
            | _ -> ()

            stream.Position <- start + size
            if size % 2L = 1L && stream.Position < stream.Length then
                stream.Position <- stream.Position + 1L

        require (audioFormat = 1us && channels = 1us && bits = 16us && sampleRate > 0u) "Cube input must be mono PCM16 WAV."

        match pcm with
        | None -> invalidArg "path" "WAV has no data chunk."
        | Some bytes when bytes.Length = 0 -> invalidArg "path" "WAV data chunk is empty."
        | Some bytes ->
            let samples = Array.zeroCreate<float> (bytes.Length / 2)
            for i = 0 to samples.Length - 1 do
                samples[i] <- float (BitConverter.ToInt16(bytes, i * 2)) / 32768.0
            samples, int sampleRate

    let private reflectSample (source: float[]) index =
        let mutable i = index
        while i < 0 || i >= source.Length do
            if i < 0 then i <- -i
            elif i >= source.Length then i <- (source.Length - 1) * 2 - i
        source[i]

    let private fft (values: Complex[]) =
        let n = values.Length
        let mutable j = 0
        for i = 1 to n - 1 do
            let mutable bit = n >>> 1
            while (j &&& bit) <> 0 do
                j <- j ^^^ bit
                bit <- bit >>> 1
            j <- j ^^^ bit
            if i < j then
                let value = values[i]
                values[i] <- values[j]
                values[j] <- value

        let mutable length = 2
        while length <= n do
            let angle = -2.0 * Math.PI / float length
            let step = Complex.FromPolarCoordinates(1.0, angle)
            for start in 0..length..n - 1 do
                let mutable twiddle = Complex.One
                for offset = 0 to length / 2 - 1 do
                    let even = values[start + offset]
                    let odd = values[start + offset + length / 2] * twiddle
                    values[start + offset] <- even + odd
                    values[start + offset + length / 2] <- even - odd
                    twiddle <- twiddle * step
            length <- length * 2

    let private stft (audio: float[]) =
        let pad = nFft / 2
        let paddedLength = audio.Length + pad * 2
        let frameCount = max 1 (1 + (paddedLength - nFft) / hop)
        let frequencies = nFft / 2 + 1
        let magnitude = Array2D.zeroCreate<float> frequencies frameCount
        for frame = 0 to frameCount - 1 do
            let windowed = Array.zeroCreate<Complex> nFft
            for sample = 0 to nFft - 1 do
                let position = frame * hop + sample - pad
                let window = 0.5 - 0.5 * Math.Cos(2.0 * Math.PI * float sample / float (nFft - 1))
                windowed[sample] <- Complex(reflectSample (audio: float[]) position * window, 0.0)
            fft windowed
            for frequency = 0 to frequencies - 1 do
                magnitude[frequency, frame] <- windowed[frequency].Magnitude
        magnitude

    let private percentile (values: float[]) quantile =
        require (values.Length > 0) "Cannot compute a percentile from empty values."
        let sorted = values |> Array.sort
        let position = quantile * float (sorted.Length - 1)
        let low = int (Math.Floor position)
        let high = min (sorted.Length - 1) (low + 1)
        sorted[low] + (position - float low) * (sorted[high] - sorted[low])

    let private mapRows (source: float[,]) transform =
        let output = Array2D.zeroCreate<float> (Array2D.length1 source) (Array2D.length2 source)
        for f = 0 to Array2D.length1 source - 1 do
            for t = 0 to Array2D.length2 source - 1 do
                output[f, t] <- transform source[f, t]
        output

    let private clamp01 value = max 0.0 (min 1.0 value)

    let private computeLayers (magnitude: float[,]) sampleRate =
        let frequencies = Array2D.length1 magnitude
        let times = Array2D.length2 magnitude
        let flatMagnitude = Array.zeroCreate<float> (frequencies * times)
        for f = 0 to frequencies - 1 do
            for t = 0 to times - 1 do
                flatMagnitude[f * times + t] <- Math.Log(1.0 + magnitude[f, t])
        let low, high = percentile flatMagnitude 0.05, percentile flatMagnitude 0.995
        let signal = Array2D.zeroCreate<float> frequencies times
        for f = 0 to frequencies - 1 do
            for t = 0 to times - 1 do
                let scaled = clamp01 ((Math.Log(1.0 + magnitude[f, t]) - low) / max (high - low) epsilon)
                signal[f, t] <- Math.Pow(scaled, 0.65)

        let tonalityPerTime = Array.zeroCreate<float> times
        let noiseFloor = Array.zeroCreate<float> frequencies
        let peakBand = Array2D.zeroCreate<float> frequencies times
        for t = 0 to times - 1 do
            let mutable logSum = 0.0
            let mutable sum = 0.0
            for f = 0 to frequencies - 1 do
                let power = magnitude[f, t] * magnitude[f, t] + epsilon
                logSum <- logSum + Math.Log power
                sum <- sum + power
            let geo = Math.Exp(logSum / float frequencies)
            tonalityPerTime[t] <- 1.0 - clamp01 (geo / (sum / float frequencies + epsilon))

        for f = 0 to frequencies - 1 do
            let row = Array.zeroCreate<float> times
            for t = 0 to times - 1 do
                row[t] <- magnitude[f, t]
            noiseFloor[f] <- percentile row 0.2 + epsilon

        for f = 0 to frequencies - 1 do
            for t = 0 to times - 1 do
                let mutable local = 0.0
                for offset = -2 to 2 do
                    let nearby = max 0 (min (frequencies - 1) (f + offset))
                    local <- local + magnitude[nearby, t]
                local <- local / 5.0
                peakBand[f, t] <- clamp01 ((magnitude[f, t] / (local + epsilon) - 1.0) / 3.0)

        let tonality = Array2D.zeroCreate<float> frequencies times
        let confidence = Array2D.zeroCreate<float> frequencies times
        for f = 0 to frequencies - 1 do
            for t = 0 to times - 1 do
                let tonal = clamp01 (0.55 * tonalityPerTime[t] + 0.45 * peakBand[f, t])
                tonality[f, t] <- if signal[f, t] > 0.08 then tonal else 0.0
                let snr = clamp01 (Math.Log(1.0 + magnitude[f, t] / noiseFloor[f]) / Math.Log 41.0)
                confidence[f, t] <- if signal[f, t] > 0.05 then snr else 0.0

        let qualityTime = Array.zeroCreate<float> times
        for t = 0 to times - 1 do
            let mutable mid = epsilon
            let mutable high = epsilon
            let mutable mud = epsilon
            for f = 0 to frequencies - 1 do
                let hz = float f * float sampleRate / float nFft
                if hz >= 300.0 && hz <= 3400.0 then mid <- mid + magnitude[f, t]
                if hz >= 4000.0 && hz <= 8000.0 then high <- high + magnitude[f, t]
                if hz > 0.0 && hz <= 200.0 then mud <- mud + magnitude[f, t]
            let clarity = high / (mid + high)
            let muddiness = mud / (mid + mud)
            qualityTime[t] <- clamp01 (0.6 * (1.0 - muddiness) + 0.4 * clarity)

        let quality = Array2D.zeroCreate<float> frequencies times
        for t = 0 to times - 1 do
            let mutable bandMax = epsilon
            for f = 0 to frequencies - 1 do
                let hz = float f * float sampleRate / float nFft
                if hz >= 200.0 && hz <= 5000.0 then bandMax <- max bandMax magnitude[f, t]
            for f = 0 to frequencies - 1 do
                let hz = float f * float sampleRate / float nFft
                let bandWeight = if hz >= 200.0 && hz <= 5000.0 then magnitude[f, t] / bandMax else 0.0
                quality[f, t] <- clamp01 (0.5 * qualityTime[t] + 0.5 * bandWeight * signal[f, t])

        dict [ "signal", signal; "tonality", tonality; "confidence", confidence; "quality", quality ]

    let private downsample (layers: IDictionary<string, float[,]>) =
        let first = layers[layerNames[0]]
        let f, t = Array2D.length1 first, Array2D.length2 first
        let sf = max 1 ((f + maxF - 1) / maxF)
        let st = max 1 ((t + maxT - 1) / maxT)
        let nf, nt = f / sf, t / st
        let sampled = Dictionary<string, float[,]>()
        for name in layerNames do
            let source = layers[name]
            let output = Array2D.zeroCreate<float> nf nt
            for fi = 0 to nf - 1 do
                for ti = 0 to nt - 1 do
                    let mutable sum = 0.0
                    for df = 0 to sf - 1 do
                        for dt = 0 to st - 1 do
                            sum <- sum + source[fi * sf + df, ti * st + dt]
                    output[fi, ti] <- sum / float (sf * st)
            sampled.Add(name, output)
        sampled, sf, st

    let private makePoints (layers: IDictionary<string, float[,]>) =
        let mutable points = ResizeArray<CubePoint>()
        for layer = 0 to layerNames.Length - 1 do
            let values = layers[layerNames[layer]]
            for f = 0 to Array2D.length1 values - 1 do
                for t = 0 to Array2D.length2 values - 1 do
                    let value = values[f, t]
                    if value >= threshold then
                        points.Add { X = t; Y = f; Z = float layer + value * 0.85; Value = value; Layer = layer }
        points.ToArray()

    let private preview (points: CubePoint[]) nf nt =
        let result = ResizeArray<JsonObject>()
        let counts = JsonObject()
        for layer = 0 to layerNames.Length - 1 do
            let candidates =
                points
                |> Array.mapi (fun index point -> index, point)
                |> Array.filter (fun (_, point) -> point.Layer = layer)
            counts[layerNames[layer]] <- JsonValue.Create candidates.Length
            let selected =
                candidates
                |> Array.sortWith (fun (leftIndex, left) (rightIndex, right) ->
                    let descending = compare right.Value left.Value
                    if descending = 0 then compare leftIndex rightIndex else descending)
                |> Array.truncate perLayer
            if selected.Length > 0 then
                let lo = selected |> Array.map (fun (_, point) -> point.Value) |> Array.min
                let hi = selected |> Array.map (fun (_, point) -> point.Value) |> Array.max
                for _, point in selected do
                    let normalized =
                        if hi <= lo then 1.0
                        else (point.Value - lo) / (hi - lo)
                    let value = JsonObject()
                    value["t"] <- JsonValue.Create(Math.Round(float point.X / float (max 1 (nt - 1)), 5, MidpointRounding.ToEven))
                    value["f"] <- JsonValue.Create(Math.Round(float point.Y / float (max 1 (nf - 1)), 5, MidpointRounding.ToEven))
                    value["z"] <- JsonValue.Create(float layer / 3.0)
                    value["v"] <- JsonValue.Create(Math.Round(normalized, 5, MidpointRounding.ToEven))
                    value["layer"] <- JsonValue.Create layerNames[layer]
                    result.Add value
        result.ToArray(), counts

    let private flatten (matrix: float[,]) =
        let width = Array2D.length2 matrix
        Array.init (Array2D.length1 matrix * width) (fun index -> matrix[index / width, index % width])

    let private layerStats (full: IDictionary<string, float[,]>) (sampled: IDictionary<string, float[,]>) =
        let result = JsonObject()
        let nf, nt = Array2D.length1 sampled[layerNames[0]], Array2D.length2 sampled[layerNames[0]]
        for name in layerNames do
            let values = flatten full[name]
            let count = float values.Length
            let mean = values |> Array.average
            let variance = values |> Array.sumBy (fun value -> let delta = value - mean in delta * delta) |> fun sum -> sum / count
            let shape = JsonArray()
            shape.Add(JsonValue.Create nf)
            shape.Add(JsonValue.Create nt)
            let stats = JsonObject()
            stats["mean"] <- JsonValue.Create mean
            stats["std"] <- JsonValue.Create(Math.Sqrt variance)
            stats["p50"] <- JsonValue.Create(percentile values 0.5)
            stats["p90"] <- JsonValue.Create(percentile values 0.9)
            stats["active_frac"] <- JsonValue.Create(values |> Array.averageBy (fun value -> if value > 0.2 then 1.0 else 0.0))
            stats["shape"] <- shape
            result[name] <- stats
        result

    let private layerScore (layers: IDictionary<string, float[,]>) =
        let mean name = flatten layers[name] |> Array.average
        0.35 * mean "signal" + 0.25 * mean "tonality" + 0.20 * mean "confidence" + 0.20 * mean "quality"

    let private invHdr audio =
        let peak = audio |> Array.map abs |> Array.max
        let rms = Math.Sqrt(audio |> Array.averageBy (fun value -> value * value))
        if peak < 1e-12 then 0.0 else rms / peak

    let private writeBigEndian (stream: Stream) (value: uint32) =
        stream.WriteByte(byte (value >>> 24))
        stream.WriteByte(byte (value >>> 16))
        stream.WriteByte(byte (value >>> 8))
        stream.WriteByte(byte value)

    let private crc32 (bytes: byte[]) =
        let mutable crc = 0xFFFFFFFFu
        for value in bytes do
            crc <- crc ^^^ uint32 value
            for _ = 0 to 7 do
                crc <- if (crc &&& 1u) <> 0u then (crc >>> 1) ^^^ 0xEDB88320u else crc >>> 1
        crc ^^^ 0xFFFFFFFFu

    let private writeChunk stream (name: string) (payload: byte[]) =
        let named = Encoding.ASCII.GetBytes name
        writeBigEndian stream (uint32 payload.Length)
        stream.Write(named, 0, named.Length)
        stream.Write(payload, 0, payload.Length)
        let checksum = Array.concat [| named; payload |]
        writeBigEndian stream (crc32 checksum)

    let private writePng path (points: CubePoint[]) title =
        let width, height = 1200, 900
        let pixels = Array.zeroCreate<byte> (width * height * 3)
        Array.fill pixels 0  pixels.Length 255uy
        let stride = max 1 ((points.Length + 79999) / 80000)
        let maxX = if points.Length = 0 then 1 else max 1 (points |> Array.maxBy (fun point -> point.X)).X
        let maxY = if points.Length = 0 then 1 else max 1 (points |> Array.maxBy (fun point -> point.Y)).Y
        for index in 0..stride..points.Length - 1 do
            let point = points[index]
            let x = int (40.0 + float point.X / float maxX * float (width - 80))
            let y = int (float (height - 40) - float point.Y / float maxY * float (height - 80))
            if x >= 0 && x < width && y >= 0 && y < height then
                let r, g, b = layerColors[point.Layer]
                let offset = (y * width + x) * 3
                pixels[offset] <- byte (r * 255.0)
                pixels[offset + 1] <- byte (g * 255.0)
                pixels[offset + 2] <- byte (b * 255.0)
        Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath path)) |> ignore
        use output = File.Create path
        output.Write([| 137uy; 80uy; 78uy; 71uy; 13uy; 10uy; 26uy; 10uy |], 0, 8)
        use ihdr = new MemoryStream()
        use ihdrWriter = new BinaryWriter(ihdr)
        ihdrWriter.Write(uint32 width |> BitConverter.GetBytes |> Array.rev)
        ihdrWriter.Write(uint32 height |> BitConverter.GetBytes |> Array.rev)
        ihdrWriter.Write(8uy)
        ihdrWriter.Write(2uy)
        ihdrWriter.Write(0uy)
        ihdrWriter.Write(0uy)
        ihdrWriter.Write(0uy)
        writeChunk output "IHDR" (ihdr.ToArray())
        use raw = new MemoryStream()
        for y = 0 to height - 1 do
            raw.WriteByte 0uy
            raw.Write(pixels, y * width * 3, width * 3)
        use compressed = new MemoryStream()
        do
            use zlib = new ZLibStream(compressed, CompressionLevel.SmallestSize, true)
            zlib.Write(raw.ToArray(), 0, int raw.Length)
        writeChunk output "IDAT" (compressed.ToArray())
        writeChunk output "IEND" [||]

    [<CompiledName("Generate")>]
    let generate (input: string) (output: string) (png: string) (stem: string) (engine: string) (label: string) (repoRoot: string) =
        let inputPath = Path.GetFullPath(input)
        let outputPath = Path.GetFullPath(output)
        let root = Path.GetFullPath(repoRoot)
        require (File.Exists inputPath) "Input WAV does not exist."
        let samples, sampleRate = readWav inputPath
        require (samples.Length > 0) "library_cube expects non-empty mono audio."
        let magnitude = stft samples
        let full = computeLayers magnitude sampleRate
        let sampled, sf, st = downsample full
        let points = makePoints sampled
        let nf, nt = Array2D.length1 sampled[layerNames[0]], Array2D.length2 sampled[layerNames[0]]
        let previewPoints, counts = preview points nf nt
        let pointArray = JsonArray()
        for point in previewPoints do pointArray.Add point
        let binSeconds = float (st * hop) / float sampleRate
        let hash = sourceHash inputPath
        let displayLabel = if String.IsNullOrWhiteSpace label then stem else label
        let doc = JsonObject()
        doc["source_wav"] <- JsonValue.Create($"artifacts/library/{stem}.wav")
        doc["source_sha256"] <- JsonValue.Create(hash)
        doc["engine"] <- JsonValue.Create(engine)
        doc["title"] <- JsonValue.Create($"Inverse-HDR cube — {displayLabel}")
        doc["cube_revision"] <- JsonValue.Create revision
        doc["sample_rate"] <- JsonValue.Create sampleRate
        doc["duration_s"] <- JsonValue.Create(float samples.Length / float sampleRate)
        doc["n_fft"] <- JsonValue.Create nFft
        doc["hop"] <- JsonValue.Create hop
        doc["inv_hdr"] <- JsonValue.Create(invHdr samples)
        doc["layer_score"] <- JsonValue.Create(layerScore full)
        doc["n_points"] <- JsonValue.Create previewPoints.Length
        doc["n_points_source"] <- JsonValue.Create points.Length
        doc["n_points_source_per_layer"] <- counts
        let cubeShape = JsonArray()
        cubeShape.Add(JsonValue.Create nf)
        cubeShape.Add(JsonValue.Create nt)
        doc["cube_shape_f_t"] <- cubeShape
        let downsampleShape = JsonArray()
        downsampleShape.Add(JsonValue.Create sf)
        downsampleShape.Add(JsonValue.Create st)
        doc["downsample_sf_st"] <- downsampleShape
        doc["bin_seconds"] <- JsonValue.Create binSeconds
        doc["cube_covers_s"] <- JsonValue.Create(float nt * binSeconds)
        doc["stft_frames"] <- JsonValue.Create(Array2D.length2 magnitude)
        let axes = JsonObject()
        axes["x"] <- JsonValue.Create "time bin"
        axes["y"] <- JsonValue.Create "freq bin"
        axes["z"] <- JsonValue.Create "layer (+value)"
        doc["axes"] <- axes
        doc["layers"] <- layerStats full sampled
        doc["points_preview"] <- pointArray
        doc["pngUrl"] <- JsonValue.Create($"/library/{stem}_cube3d.png")
        doc["wavUrl"] <- JsonValue.Create($"/library/{stem}.wav")
        let legend = JsonObject()
        legend["signal"] <- JsonValue.Create "blue"
        legend["tonality"] <- JsonValue.Create "green"
        legend["confidence"] <- JsonValue.Create "orange"
        legend["quality"] <- JsonValue.Create "pink"
        doc["legend"] <- legend
        let parameters = JsonObject()
        parameters["n_fft"] <- JsonValue.Create nFft
        parameters["hop"] <- JsonValue.Create hop
        parameters["max_f"] <- JsonValue.Create maxF
        parameters["max_t"] <- JsonValue.Create maxT
        parameters["thresh"] <- JsonValue.Create threshold
        parameters["per_layer"] <- JsonValue.Create perLayer
        let provenance = JsonObject()
        provenance["generator"] <- JsonValue.Create generatorPath
        provenance["generator_sha256"] <- JsonValue.Create(generatorHash root)
        provenance["layer_method"] <- JsonValue.Create layerMethod
        provenance["params"] <- parameters
        doc["provenance"] <- provenance

        Directory.CreateDirectory(Path.GetDirectoryName outputPath) |> ignore
        File.WriteAllText(outputPath, doc.ToJsonString(), UTF8Encoding(false))
        let pngPath =
            if String.IsNullOrWhiteSpace png then
                None
            else
                let path = Path.GetFullPath png
                writePng path points $"Inverse-HDR cube — {displayLabel}"
                Some path
        { OutputPath = outputPath
          PngPath = pngPath
          SourceSha256 = hash
          PointCount = previewPoints.Length
          CubeShape = nf, nt
          DurationSeconds = float samples.Length / float sampleRate
          InvHdr = invHdr samples }
