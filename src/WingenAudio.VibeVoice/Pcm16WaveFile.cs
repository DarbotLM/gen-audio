using WingenAudio.Contracts;

namespace WingenAudio.VibeVoice;

public sealed class Pcm16WaveFile
{
    private static readonly int[] AcceptedSampleRates = [16000, 22050, 24000, 44100, 48000];

    private Pcm16WaveFile(VibeVoiceWaveInfo info, float[] normalizedSamples, float[] samples24Khz)
    {
        Info = info;
        NormalizedSamples = normalizedSamples;
        Samples24Khz = samples24Khz;
    }

    public VibeVoiceWaveInfo Info { get; }

    public IReadOnlyList<float> NormalizedSamples { get; }

    public float[] Samples24Khz { get; }

    public static Pcm16WaveFile Read(string path, string? confinementRoot = null)
    {
        string fullPath = Path.GetFullPath(path);
        if (confinementRoot is not null)
        {
            string root = Path.GetFullPath(confinementRoot);
            string relative = Path.GetRelativePath(root, fullPath);
            if (relative == ".." ||
                relative.StartsWith($"..{Path.DirectorySeparatorChar}", StringComparison.Ordinal) ||
                Path.IsPathRooted(relative))
            {
                throw new IOException($"WAV reference escapes confinement root: {path}");
            }
        }
        var file = new FileInfo(fullPath);
        if (!file.Exists ||
            (file.Attributes & FileAttributes.ReparsePoint) != 0 ||
            file.Length is > 16 * 1024 * 1024 or < 44)
        {
            throw new InvalidDataException(
                $"PCM WAV must be a regular file between 44 bytes and 16 MiB: {path}");
        }

        using var stream = new FileStream(
            fullPath,
            FileMode.Open,
            FileAccess.Read,
            FileShare.Read,
            64 * 1024);
        return Read(stream, fullPath);
    }

    public static Pcm16WaveFile Read(Stream stream, string displayPath)
    {
        ArgumentNullException.ThrowIfNull(stream);
        if (!stream.CanRead || !stream.CanSeek)
        {
            throw new ArgumentException("WAV stream must be readable and seekable.");
        }
        stream.Position = 0;
        if (stream.Length is > 16 * 1024 * 1024 or < 44)
        {
            throw new InvalidDataException(
                $"PCM WAV stream must be between 44 bytes and 16 MiB: {displayPath}");
        }
        using var reader = new BinaryReader(stream);
        if (new string(reader.ReadChars(4)) != "RIFF" ||
            reader.ReadUInt32() + 8 != reader.BaseStream.Length ||
            new string(reader.ReadChars(4)) != "WAVE")
        {
            throw new InvalidDataException("Stream is not a complete RIFF/WAVE container.");
        }

        int sampleRate = 0;
        short channels = 0;
        short bitsPerSample = 0;
        short audioFormat = 0;
        byte[]? pcm = null;
        while (stream.Position + 8 <= stream.Length)
        {
            string chunk = new(reader.ReadChars(4));
            uint length = reader.ReadUInt32();
            if (length > stream.Length - stream.Position)
            {
                throw new InvalidDataException($"WAV chunk '{chunk}' is truncated.");
            }
            long next = stream.Position + length;
            if (chunk == "fmt ")
            {
                if (length < 16)
                {
                    throw new InvalidDataException("WAV fmt chunk is truncated.");
                }
                audioFormat = reader.ReadInt16();
                channels = reader.ReadInt16();
                sampleRate = reader.ReadInt32();
                reader.ReadInt32();
                reader.ReadInt16();
                bitsPerSample = reader.ReadInt16();
                stream.Position = next;
            }
            else if (chunk == "data")
            {
                if (length > int.MaxValue)
                {
                    throw new InvalidDataException("WAV data chunk is too large.");
                }
                pcm = reader.ReadBytes(checked((int)length));
            }
            else
            {
                stream.Position = next;
            }
            if ((length & 1) != 0 && stream.Position < stream.Length)
            {
                stream.Position++;
            }
        }

        if (audioFormat != 1 ||
            channels != 1 ||
            bitsPerSample != 16 ||
            pcm is null ||
            pcm.Length == 0 ||
            pcm.Length % 2 != 0 ||
            !AcceptedSampleRates.Contains(sampleRate))
        {
            throw new InvalidDataException(
                "WAV must be mono PCM16 at 16, 22.05, 24, 44.1, or 48 kHz.");
        }

        int sampleCount = pcm.Length / 2;
        double duration = sampleCount / (double)sampleRate;
        if (duration is < 0.5 or > 30.0)
        {
            throw new InvalidDataException(
                $"Reference duration {duration:F3}s is outside 0.5..30 seconds.");
        }

        var source = new float[sampleCount];
        double peak = 0;
        for (int index = 0; index < sampleCount; index++)
        {
            short sample = BitConverter.ToInt16(pcm, index * 2);
            float normalized = sample / 32768f;
            source[index] = normalized;
            peak = Math.Max(peak, Math.Abs(normalized));
        }
        float[] target = ResampleLinear(source, sampleRate, 24000);
        var info = new VibeVoiceWaveInfo(
            displayPath,
            sampleRate,
            24000,
            channels,
            bitsPerSample,
            duration,
            sampleCount,
            target.Length,
            peak);
        return new(info, source, target);
    }

    public static float[] ResampleLinear(
        IReadOnlyList<float> source,
        int sourceRate,
        int targetRate)
    {
        if (!AcceptedSampleRates.Contains(sourceRate) || targetRate != 24000)
        {
            throw new ArgumentException("Unsupported WAV resampling conversion.");
        }
        if (sourceRate == targetRate)
        {
            return [.. source];
        }
        int outputLength = checked((int)Math.Round(
            source.Count * (targetRate / (double)sourceRate)));
        if (outputLength <= 0)
        {
            return [];
        }
        var output = new float[outputLength];
        double step = sourceRate / (double)targetRate;
        for (int index = 0; index < outputLength; index++)
        {
            double position = index * step;
            int lower = Math.Min((int)position, source.Count - 1);
            int upper = Math.Min(lower + 1, source.Count - 1);
            float fraction = (float)(position - lower);
            output[index] = source[lower] +
                ((source[upper] - source[lower]) * fraction);
        }
        return output;
    }
}
