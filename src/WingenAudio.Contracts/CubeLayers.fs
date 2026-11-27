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
      SpectrogramPath: string option
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
            if not (bytes[i] = byte '\r' && i + 1 < bytes.Length && bytes[i + 1] = byte '\n') then
                count <- count + 1

        let normalized = Array.zeroCreate<byte> count
        let mutable target = 0

        for i = 0 to bytes.Length - 1 do
            if not (bytes[i] = byte '\r' && i + 1 < bytes.Length && bytes[i + 1] = byte '\n') then
                normalized[target] <- bytes[i]
                target <- target + 1

        normalized

    let private sha256Bytes (bytes: byte[]) =
        Convert.ToHexString(SHA256.HashData(bytes)).ToLowerInvariant()

    let private sourceHash path = sha256Bytes (File.ReadAllBytes path)

    let private generatorHash repoRoot =
        use source = typeof<CubePoint>.Assembly.GetManifestResourceStream("WingenAudio.Contracts.CubeLayers.fs")
        require (not (isNull source)) "Native cube source resource is missing."
        use bytes = new MemoryStream()
        source.CopyTo bytes
        bytes.ToArray() |> normalizeLineEndings |> sha256Bytes

    let private readWav path maxSeconds =
        use stream = File.OpenRead path
        require (stream.Length >= 44L && stream.Length <= 268435456L) "WAV exceeds its 256 MiB bound or is truncated."
        use reader = new BinaryReader(stream, Encoding.ASCII, leaveOpen = false)
        require (Encoding.ASCII.GetString(reader.ReadBytes 4) = "RIFF") "WAV is missing a RIFF header."
        require (int64 (reader.ReadUInt32()) + 8L = stream.Length) "WAV RIFF length does not match the file."
        require (Encoding.ASCII.GetString(reader.ReadBytes 4) = "WAVE") "WAV is missing a WAVE header."

        let mutable audioFormat = 0us
        let mutable channels = 0us
        let mutable sampleRate = 0u
        let mutable bits = 0us
        let mutable blockAlign = 0us
        let mutable byteRate = 0u
        let mutable hasFormat = false
        let mutable pcm: byte[] option = None

        while stream.Position + 8L <= stream.Length do
            let id = Encoding.ASCII.GetString(reader.ReadBytes 4)
            let size = int64 (reader.ReadUInt32())
            let start = stream.Position
            require (start + size + size % 2L <= stream.Length) "WAV chunk exceeds the file."

            match id with
            | "fmt " when size >= 16L ->
                require (not hasFormat) "Duplicate WAV format."
                hasFormat <- true
                audioFormat <- reader.ReadUInt16()
                channels <- reader.ReadUInt16()
                sampleRate <- reader.ReadUInt32()
                byteRate <- reader.ReadUInt32()
                blockAlign <- reader.ReadUInt16()
                bits <- reader.ReadUInt16()
                if audioFormat = 65534us then
                    require (size >= 40L && reader.ReadUInt16() = 22us && reader.ReadUInt16() = bits) "Unsupported extensible WAV."
                    let mask = reader.ReadUInt32()
                    require (mask = 0u || mask = (if channels = 1us then 4u else 3u)) "Unsupported channel layout."
                    let subtype = reader.ReadUInt32()
                    require (subtype = 1u || subtype = 3u) "Unsupported WAV subtype."
                    require (reader.ReadBytes(12) = [|0uy;0uy;16uy;0uy;128uy;0uy;0uy;170uy;0uy;56uy;155uy;113uy|]) "Invalid WAV subtype GUID."
                    audioFormat <- uint16 subtype
            | "data" ->
                require pcm.IsNone "Duplicate WAV data."
                pcm <- Some(reader.ReadBytes(int size))
            | _ -> ()

            stream.Position <- start + size
            if size % 2L = 1L && stream.Position < stream.Length then
                stream.Position <- stream.Position + 1L

        require (stream.Position = stream.Length) "Trailing partial WAV chunk."
        require (hasFormat && ((audioFormat = 1us && bits = 16us) || (audioFormat = 3us && bits = 32us))
                 && (channels = 1us || channels = 2us) && sampleRate >= 8000u && sampleRate <= 192000u
                 && blockAlign = channels * (bits / 8us) && byteRate = sampleRate * uint32 blockAlign)
                "Cube input must be mono/stereo PCM16 or float32 WAV."

        match pcm with
        | None -> invalidArg "path" "WAV has no data chunk."
        | Some bytes when bytes.Length = 0 -> invalidArg "path" "WAV data chunk is empty."
        | Some bytes ->
            require (bytes.Length % int blockAlign = 0) "WAV data is not frame-aligned."
            let frameCount = bytes.Length / int blockAlign
            require (int64 frameCount <= int64 sampleRate * maxSeconds) "Analysis input exceeds its duration bound."
            let samples = Array.zeroCreate<float> frameCount
            let sums = Array.zeroCreate<float> (int channels)
            let energies = Array.zeroCreate<float> (int channels)
            let peaks = Array.zeroCreate<float> (int channels)
            let clipped = Array.zeroCreate<int> (int channels)
            for i = 0 to samples.Length - 1 do
                for channel = 0 to int channels - 1 do
                    let offset = i * int blockAlign + channel * int (bits / 8us)
                    let value = if audioFormat = 1us then float (BitConverter.ToInt16(bytes, offset)) / 32768.0 else float (BitConverter.ToSingle(bytes, offset))
                    require (Double.IsFinite value) "PCM contains non-finite samples."
                    samples[i] <- samples[i] + value / float channels
                    sums[channel] <- sums[channel] + value
                    energies[channel] <- energies[channel] + value * value
                    peaks[channel] <- max peaks[channel] (abs value)
                    if abs value >= 1.0 then clipped[channel] <- clipped[channel] + 1
            let metrics = JsonArray()
            for channel = 0 to int channels - 1 do
                let metric = JsonObject()
                metric["channel"] <- JsonValue.Create channel
                metric["rms"] <- JsonValue.Create(Math.Sqrt(energies[channel] / float samples.Length))
                metric["sample_peak"] <- JsonValue.Create peaks[channel]
                metric["dc"] <- JsonValue.Create(sums[channel] / float samples.Length)
                metric["full_scale_fraction"] <- JsonValue.Create(float clipped[channel] / float samples.Length)
                metrics.Add metric
            samples, int sampleRate, metrics

    let private reflectSample (source: float[]) index =
        if source.Length = 1 then source[0]
        else
            let period = 2 * (source.Length - 1)
            let i = ((index % period) + period) % period
            source[if i < source.Length then i else period - i]

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
        let baseFrames = max 1 (1 + (paddedLength - nFft) / hop)
        let actualHop = hop * max 1 ((baseFrames + 5999) / 6000)
        let frameCount = max 1 (1 + (paddedLength - nFft) / actualHop)
        let frequencies = nFft / 2 + 1
        let magnitude = Array2D.zeroCreate<float> frequencies frameCount
        for frame = 0 to frameCount - 1 do
            let windowed = Array.zeroCreate<Complex> nFft
            for sample = 0 to nFft - 1 do
                let position = frame * actualHop + sample - pad
                let window = 0.5 - 0.5 * Math.Cos(2.0 * Math.PI * float sample / float (nFft - 1))
                windowed[sample] <- Complex(reflectSample (audio: float[]) position * window, 0.0)
            fft windowed
            for frequency = 0 to frequencies - 1 do
                magnitude[frequency, frame] <- windowed[frequency].Magnitude
        magnitude, actualHop

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
                    value["z"] <- JsonValue.Create(Math.Round(point.Z / 3.85, 5, MidpointRounding.ToEven))
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

    let private savePng path width height (pixels: byte[]) =
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

    let private glyphs =
        [| 'A',"0E11111F111111"; 'B',"1E11111E11111E"; 'C',"0E11101010110E"; 'D',"1E11111111111E"
           'E',"1F10101E10101F"; 'F',"1F10101E101010"; 'G',"0E11101711110F"; 'H',"1111111F111111"
           'I',"0E04040404040E"; 'J',"0702020212120C"; 'K',"11121418141211"; 'L',"1010101010101F"
           'M',"111B1515111111"; 'N',"11191513111111"; 'O',"0E11111111110E"; 'P',"1E11111E101010"
           'Q',"0E11111115120D"; 'R',"1E11111E141211"; 'S',"0F10100E01011E"; 'T',"1F040404040404"
           'U',"1111111111110E"; 'V',"11111111110A04"; 'W',"11111115151B11"; 'X',"11110A040A1111"
           'Y',"11110A04040404"; 'Z',"1F01020408101F"; '0',"0E11131519110E"; '1',"040C040404040E"
           '2',"0E11010204081F"; '3',"1E01010E01011E"; '4',"02060A121F0202"; '5',"1F10101E01011E"
           '6',"0E10101E11110E"; '7',"1F010204080808"; '8',"0E11110E11110E"; '9',"0E11110F01010E"
           '-',"0000001F000000"; '.',"00000000000C0C"; '+',"0004041F040400"; ':',"000C0C000C0C00"
           '(',"02040808080402"; ')',"08040202020408"; '/',"01010204081010"; '_',"0000000000001F"
           '=',"00001F001F0000" |]
        |> Array.map (fun (letter, rows) -> letter, Convert.FromHexString rows)
        |> Map.ofArray

    let private canvas width height =
        let pixels = Array.create (width * height * 3) 250uy
        let pixel x y (r, g, b) =
            if x >= 0 && y >= 0 && x < width && y < height then
                let offset = (y * width + x) * 3
                pixels[offset] <- byte r
                pixels[offset + 1] <- byte g
                pixels[offset + 2] <- byte b
        let line (x1, y1) (x2, y2) color =
            let steps = max 1 (max (abs (x2 - x1)) (abs (y2 - y1)))
            for step = 0 to steps do
                pixel (x1 + (x2 - x1) * step / steps) (y1 + (y2 - y1) * step / steps) color
        let text x y scale (value: string) color =
            for index = 0 to value.Length - 1 do
                match Map.tryFind (Char.ToUpperInvariant value[index]) glyphs with
                | None -> ()
                | Some rows ->
                    for row = 0 to 6 do
                        for column = 0 to 4 do
                            if (rows[row] &&& (1uy <<< (4 - column))) <> 0uy then
                                for dy = 0 to scale - 1 do
                                    for dx = 0 to scale - 1 do
                                        pixel (x + (index * 6 + column) * scale + dx) (y + row * scale + dy) color
        pixels, pixel, line, text

    let private writePng path (points: CubePoint[]) nf nt (duration: float) (maxFrequency: float) title =
        let pixels, pixel, line, text = canvas 1200 900
        let project t f z = int (140.0 + t * 760.0 + f * 170.0), int (760.0 - f * 240.0 - z * 410.0)
        let ink, grid = (35, 45, 60), (205, 213, 225)
        for layer = 0 to 3 do
            let z = float layer / 3.85
            for step = 0 to 4 do
                let value = float step / 4.0
                line (project value 0.0 z) (project value 1.0 z) grid
                line (project 0.0 value z) (project 1.0 value z) grid
        line (project 0.0 0.0 0.0) (project 1.0 0.0 0.0) ink
        line (project 0.0 0.0 0.0) (project 0.0 1.0 0.0) ink
        line (project 0.0 0.0 0.0) (project 0.0 0.0 1.0) ink
        let ordered = points |> Array.sortBy (fun point -> -point.Y, point.Z, point.X)
        let stride = max 1 ((ordered.Length + 79999) / 80000)
        for index in 0..stride..ordered.Length - 1 do
            let point = ordered[index]
            let x, y = project (float point.X / float (max 1 (nt - 1))) (float point.Y / float (max 1 (nf - 1))) (point.Z / 3.85)
            let r, g, b = layerColors[point.Layer]
            let intensity = 0.45 + 0.55 * point.Value
            let color = int (r * intensity * 255.0), int (g * intensity * 255.0), int (b * intensity * 255.0)
            pixel x y color
            pixel (x + 1) y color
        text 50 25 3 "BITCUBE 3D" ink
        text 50 60 2 (title |> Seq.truncate 60 |> Seq.toArray |> String) ink
        text 410 825 2 "TIME (S)" ink
        for y = 475 to 507 do line (150, y) (337, y) (250, 250, 250)
        for y = 510 to 535 do line (238, y) (316, y) (250, 250, 250)
        text 160 485 2 "FREQUENCY (HZ)" ink
        text 25 305 2 "LAYER + VALUE" ink
        for step = 0 to 4 do
            let t = float step / 4.0
            let x, y = project t 0.0 0.0
            text (x - 10) (y + 15) 2 ((duration * t).ToString("0.0", Globalization.CultureInfo.InvariantCulture)) ink
        text 245 515 2 (maxFrequency.ToString("0", Globalization.CultureInfo.InvariantCulture)) ink
        for y = 90 to 225 do line (915, y) (1190, y) (250, 250, 250)
        for layer = 0 to 3 do
            let r, g, b = layerColors[layer]
            let color = int (r * 255.0), int (g * 255.0), int (b * 255.0)
            for y = 0 to 11 do
                line (925, 100 + layer * 30 + y) (945, 100 + layer * 30 + y) color
            text 955 (100 + layer * 30) 2 layerNames[layer] ink
            let _, y = project 0.0 0.0 (float layer / 3.85)
            text 115 (y - 7) 2 (string layer) ink
        text 50 870 1 "HEURISTIC METRICS - NOT SPEAKER IDENTITY OR A LOUDNESS CERTIFICATE" ink
        savePng path 1200 900 pixels

    let private writeSpectrogram path (magnitude: float[,]) sampleRate duration =
        let pixels, pixel, line, text = canvas 1200 760
        let ink = 35, 45, 60
        let color value =
            let v = clamp01 value
            int (255.0 * clamp01 (3.0 * v - 1.0)), int (230.0 * Math.Sin(Math.PI * v / 2.0)), int (220.0 * (1.0 - v))
        let nf, nt = Array2D.length1 magnitude, Array2D.length2 magnitude
        for x = 0 to 939 do
            let t = min (nt - 1) (x * nt / 940)
            for y = 0 to 539 do
                let f = min (nf - 1) ((539 - y) * nf / 540)
                let scale = (if f = 0 || f = nf - 1 then 2.0 else 4.0) / float (nFft - 1)
                let db = 20.0 * Math.Log10(max 1e-6 (magnitude[f, t] * scale))
                pixel (100 + x) (100 + y) (color ((db + 100.0) / 100.0))
        for y = 0 to 539 do
            line (1070, 100 + y) (1090, 100 + y) (color (1.0 - float y / 539.0))
        text 45 25 3 "SPECTROGRAM" ink
        text 45 65 2 "HANN WINDOW - ONE-SIDED AMPLITUDE DBFS" ink
        text 420 705 2 "TIME (S)" ink
        text 10 70 2 "HZ" ink
        for step = 0 to 4 do
            text (85 + step * 235) 660 2 ((duration * float step / 4.0).ToString("0.0", Globalization.CultureInfo.InvariantCulture)) ink
            text 5 (630 - step * 135) 2 (string (sampleRate * step / 8)) ink
        for step = 0 to 5 do
            text 1100 (100 + step * 106) 2 (string (-20 * step)) ink
        text 100 737 1 "ANALYSIS DOWNMIX ONLY - ORIGINAL AUDIO CHANNELS ARE UNCHANGED" ink
        savePng path 1200 760 pixels

    let private generateCore (input: string) (output: string) (png: string) (spectrogram: string) (stem: string) (engine: string) (label: string) (repoRoot: string) =
        let inputPath = Path.GetFullPath(input)
        let outputPath = Path.GetFullPath(output)
        let root = Path.GetFullPath(repoRoot)
        require (File.Exists inputPath) "Input WAV does not exist."
        let samples, sampleRate, channelMetrics = readWav inputPath (if String.IsNullOrWhiteSpace spectrogram then 600L else 60L)
        require (samples.Length > 0) "library_cube expects non-empty audio."
        let magnitude, actualHop = stft samples
        let full = computeLayers magnitude sampleRate
        let sampled, sf, st = downsample full
        let points = makePoints sampled
        let nf, nt = Array2D.length1 sampled[layerNames[0]], Array2D.length2 sampled[layerNames[0]]
        let previewPoints, counts = preview points nf nt
        let pointArray = JsonArray()
        for point in previewPoints do pointArray.Add point
        let binSeconds = float (st * actualHop) / float sampleRate
        let hash = sourceHash inputPath
        let displayLabel = if String.IsNullOrWhiteSpace label then stem else label
        let doc = JsonObject()
        doc["source_wav"] <- JsonValue.Create(Path.GetFileName inputPath)
        doc["source_sha256"] <- JsonValue.Create(hash)
        doc["engine"] <- JsonValue.Create(engine)
        doc["title"] <- JsonValue.Create($"Inverse-HDR cube — {displayLabel}")
        doc["cube_revision"] <- JsonValue.Create(if String.IsNullOrWhiteSpace spectrogram then revision else 4)
        doc["sample_rate"] <- JsonValue.Create sampleRate
        doc["duration_s"] <- JsonValue.Create(float samples.Length / float sampleRate)
        doc["n_fft"] <- JsonValue.Create nFft
        doc["hop"] <- JsonValue.Create actualHop
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
        if String.IsNullOrWhiteSpace spectrogram then
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
        parameters["hop"] <- JsonValue.Create actualHop
        parameters["max_stft_frames"] <- JsonValue.Create 6000
        parameters["max_f"] <- JsonValue.Create maxF
        parameters["max_t"] <- JsonValue.Create maxT
        parameters["thresh"] <- JsonValue.Create threshold
        parameters["per_layer"] <- JsonValue.Create perLayer
        let provenance = JsonObject()
        provenance["generator"] <- JsonValue.Create generatorPath
        provenance["generator_sha256"] <- JsonValue.Create(generatorHash root)
        provenance["layer_method"] <- JsonValue.Create(if String.IsNullOrWhiteSpace spectrogram then layerMethod else "native_bitcube_r4")
        provenance["params"] <- parameters
        doc["provenance"] <- provenance
        let analysis = JsonObject()
        analysis["channels"] <- channelMetrics
        analysis["downmix"] <- JsonValue.Create(if channelMetrics.Count = 1 then "none" else "arithmetic mean for analysis only; source unchanged")
        let monoRms = Math.Sqrt(samples |> Array.averageBy (fun value -> value * value))
        let maxPeak = channelMetrics |> Seq.map (fun node -> node["sample_peak"].GetValue<float>()) |> Seq.max
        let channelRms = Math.Sqrt(channelMetrics |> Seq.averageBy (fun node -> let rms = node["rms"].GetValue<float>() in rms * rms))
        analysis["downmix_rms"] <- JsonValue.Create monoRms
        analysis["downmix_cancellation_defined"] <- JsonValue.Create(channelRms > 1e-12)
        analysis["downmix_cancellation_db"] <- JsonValue.Create(if channelRms <= 1e-12 then 0.0 else 20.0 * Math.Log10(max 1e-6 (monoRms / channelRms)))
        let recommended = if maxPeak <= 0.5011872336 then 0 else int (Math.Floor(20000.0 * Math.Log10(0.5011872336 / maxPeak)))
        analysis["recommended_gain_mdb"] <- JsonValue.Create(max -60000 recommended)
        analysis["recommendation_basis"] <- JsonValue.Create "Attenuation-only starting point for -6 dBFS sample headroom; not LUFS/true peak, speaker identity, or a quality guarantee."
        let activity = JsonArray()
        let step = max 1 (sampleRate / 10)
        for offset in 0..step..samples.Length - 1 do
            let count = min step (samples.Length - offset)
            let mutable energy = 0.0
            for index = offset to offset + count - 1 do energy <- energy + samples[index] * samples[index]
            let item = JsonObject()
            item["start_ms"] <- JsonValue.Create(int64 offset * 1000L / int64 sampleRate)
            item["rms"] <- JsonValue.Create(Math.Sqrt(energy / float count))
            activity.Add item
        analysis["activity_100ms"] <- activity
        doc["analysis"] <- analysis

        Directory.CreateDirectory(Path.GetDirectoryName outputPath) |> ignore
        File.WriteAllText(outputPath, doc.ToJsonString(), UTF8Encoding(false))
        let pngPath =
            if String.IsNullOrWhiteSpace png then
                None
            else
                let path = Path.GetFullPath png
                writePng path points nf nt (float samples.Length / float sampleRate) (float (nf * sf - 1) * float sampleRate / float nFft) displayLabel
                Some path
        let spectrogramPath =
            if String.IsNullOrWhiteSpace spectrogram then None
            else
                let path = Path.GetFullPath spectrogram
                writeSpectrogram path magnitude sampleRate (float samples.Length / float sampleRate)
                Some path
        { OutputPath = outputPath
          PngPath = pngPath
          SpectrogramPath = spectrogramPath
          SourceSha256 = hash
          PointCount = previewPoints.Length
          CubeShape = nf, nt
          DurationSeconds = float samples.Length / float sampleRate
          InvHdr = invHdr samples }

    [<CompiledName("Generate")>]
    let generate input output png stem engine label repoRoot =
        generateCore input output png "" stem engine label repoRoot

    [<CompiledName("Analyze")>]
    let analyze input output png spectrogram stem repoRoot =
        generateCore input output png spectrogram stem "native_analysis" stem repoRoot
