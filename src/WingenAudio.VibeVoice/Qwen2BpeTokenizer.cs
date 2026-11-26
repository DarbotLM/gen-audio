using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace WingenAudio.VibeVoice;

public sealed class Qwen2BpeTokenizer
{
    private const string SystemPrompt =
        " Transform the text provided by various speakers into speech output, utilizing the distinct voice of each respective speaker.\n";
    private static readonly Regex Pretokenizer = new(
        "(?i:'s|'t|'re|'ve|'m|'ll|'d)|[^\\r\\n\\p{L}\\p{N}]?\\p{L}+|\\p{N}| ?[^\\s\\p{L}\\p{N}]+[\\r\\n]*|\\s*[\\r\\n]+|\\s+(?!\\S)|\\s+",
        RegexOptions.CultureInvariant | RegexOptions.Compiled);
    private static readonly char[] ByteAlphabet = CreateByteAlphabet();
    private readonly Dictionary<string, int> _vocabulary;
    private readonly Dictionary<string, int> _mergeRanks;
    private readonly Dictionary<string, int> _addedTokens;
    private readonly int _unknownId;

    private Qwen2BpeTokenizer(
        Dictionary<string, int> vocabulary,
        Dictionary<string, int> mergeRanks,
        Dictionary<string, int> addedTokens,
        int unknownId)
    {
        _vocabulary = vocabulary;
        _mergeRanks = mergeRanks;
        _addedTokens = addedTokens;
        _unknownId = unknownId;
    }

    public int VocabularySize => _vocabulary.Count + _addedTokens.Count;

    public int EndOfTextId { get; private init; }

    public int SpeechStartId { get; private init; }

    public int SpeechEndId { get; private init; }

    public int SpeechDiffusionId { get; private init; }

    public static Qwen2BpeTokenizer Load(string tokenizerDirectory)
    {
        string root = Path.GetFullPath(tokenizerDirectory);
        using JsonDocument tokenizerJson = JsonDocument.Parse(
            File.ReadAllBytes(Path.Combine(root, "tokenizer.json")));
        using JsonDocument vocabularyJson = JsonDocument.Parse(
            File.ReadAllBytes(Path.Combine(root, "vocab.json")));
        using JsonDocument configJson = JsonDocument.Parse(
            File.ReadAllBytes(Path.Combine(root, "tokenizer_config.json")));
        string mergesPath = Path.Combine(root, "merges.txt");
        if (!File.Exists(mergesPath))
        {
            throw new FileNotFoundException("Qwen2 tokenizer merges.txt was not found.", mergesPath);
        }

        JsonElement model = Required(tokenizerJson.RootElement, "model");
        if (!string.Equals(
                model.GetProperty("type").GetString(),
                "BPE",
                StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidDataException("tokenizer.json is not a BPE tokenizer.");
        }

        var vocabulary = ReadVocabulary(vocabularyJson.RootElement);
        var tokenizerVocabulary = ReadVocabulary(Required(model, "vocab"));
        if (vocabulary.Count != tokenizerVocabulary.Count ||
            vocabulary.Any(entry =>
                !tokenizerVocabulary.TryGetValue(entry.Key, out int id) || id != entry.Value))
        {
            throw new InvalidDataException(
                "vocab.json does not exactly match tokenizer.json model vocabulary.");
        }

        string[] mergeLines = File.ReadAllLines(mergesPath)
            .Where(line => !string.IsNullOrWhiteSpace(line) && !line.StartsWith('#'))
            .Select(line => line.Trim())
            .ToArray();
        JsonElement merges = Required(model, "merges");
        var jsonMerges = merges.EnumerateArray()
            .Select(item => item.GetString()!)
            .ToArray();
        var jsonMergeSet = jsonMerges.ToHashSet(StringComparer.Ordinal);
        if (mergeLines.Length < 100_000 ||
            mergeLines.Any(line => !jsonMergeSet.Contains(line)))
        {
            throw new InvalidDataException(
                "merges.txt is incomplete or contains merges absent from tokenizer.json.");
        }
        var mergeRanks = new Dictionary<string, int>(StringComparer.Ordinal);
        for (int index = 0; index < jsonMerges.Length; index++)
        {
            mergeRanks.TryAdd(jsonMerges[index], index);
        }

        var addedTokens = new Dictionary<string, int>(StringComparer.Ordinal);
        foreach (JsonElement item in Required(tokenizerJson.RootElement, "added_tokens").EnumerateArray())
        {
            string content = Required(item, "content").GetString()!;
            int id = Required(item, "id").GetInt32();
            addedTokens.Add(content, id);
        }

        string unknown = model.TryGetProperty("unk_token", out JsonElement unk) &&
            unk.ValueKind == JsonValueKind.String
                ? unk.GetString() ?? "<|endoftext|>"
                : "<|endoftext|>";
        int unknownId = addedTokens.TryGetValue(unknown, out int configuredUnknown)
            ? configuredUnknown
            : vocabulary.TryGetValue(unknown, out int vocabularyUnknown)
                ? vocabularyUnknown
                : addedTokens["<|endoftext|>"];
        var tokenizer = new Qwen2BpeTokenizer(
            vocabulary,
            mergeRanks,
            addedTokens,
            unknownId)
        {
            EndOfTextId = SpecialId(addedTokens, "<|endoftext|>"),
            SpeechStartId = SpecialId(addedTokens, "<|vision_start|>"),
            SpeechEndId = SpecialId(addedTokens, "<|vision_end|>"),
            SpeechDiffusionId = SpecialId(addedTokens, "<|vision_pad|>"),
        };
        tokenizer.ValidateConfig(configJson.RootElement);
        return tokenizer;
    }

    public int[] Encode(string text)
    {
        ArgumentNullException.ThrowIfNull(text);
        var result = new List<int>();
        int position = 0;
        while (position < text.Length)
        {
            KeyValuePair<string, int>? addedToken = _addedTokens
                .Where(entry => position + entry.Key.Length <= text.Length &&
                    string.CompareOrdinal(
                        text,
                        position,
                        entry.Key,
                        0,
                        entry.Key.Length) == 0)
                .OrderByDescending(entry => entry.Key.Length)
                .Cast<KeyValuePair<string, int>?>()
                .FirstOrDefault();
            if (addedToken.HasValue)
            {
                result.Add(addedToken.Value.Value);
                position += addedToken.Value.Key.Length;
                continue;
            }

            Match match = Pretokenizer.Match(text, position);
            if (!match.Success || match.Index != position)
            {
                int length = char.IsHighSurrogate(text[position]) &&
                    position + 1 < text.Length &&
                    char.IsLowSurrogate(text[position + 1])
                    ? 2
                    : 1;
                string fallback = text.Substring(position, length);
                EncodeWord(fallback, result);
                position += fallback.Length;
                continue;
            }
            EncodeWord(match.Value, result);
            position += match.Length;
        }
        return [.. result];
    }

    public VibeVoicePrompt TokenizeScript(string script, IReadOnlyList<float[]> references24Khz)
    {
        VibeVoiceSpeakerScript parsed = VibeVoiceSpeakerScript.Parse(script);
        int speakerCount = parsed.Speakers
            .Select(line => line.Speaker)
            .Distinct()
            .Count();
        if (references24Khz.Count != speakerCount)
        {
            throw new ArgumentException(
                $"Reference count {references24Khz.Count} must match speaker count {speakerCount}.");
        }

        var ids = new List<int>();
        var speechMask = new List<bool>();
        AppendEncoded(ids, speechMask, Encode(SystemPrompt));
        AppendEncoded(ids, speechMask, Encode(" Voice input:\n"));
        for (int index = 0; index < references24Khz.Count; index++)
        {
            AppendEncoded(
                ids,
                speechMask,
                Encode($" Speaker {index}:"));
            ids.Add(SpeechStartId);
            speechMask.Add(false);
            int latentTokens = checked((references24Khz[index].Length + 3199) / 3200);
            ids.AddRange(Enumerable.Repeat(SpeechDiffusionId, latentTokens));
            speechMask.AddRange(Enumerable.Repeat(true, latentTokens));
            ids.Add(SpeechEndId);
            speechMask.Add(false);
            AppendEncoded(ids, speechMask, Encode("\n"));
        }
        AppendEncoded(ids, speechMask, Encode(" Text input:\n"));
        foreach (VibeVoiceSpeakerLine line in parsed.Speakers)
        {
            AppendEncoded(
                ids,
                speechMask,
                Encode($" Speaker {line.Speaker - 1}: {line.Text}\n"));
        }
        AppendEncoded(ids, speechMask, Encode(" Speech output:\n"));
        ids.Add(SpeechStartId);
        speechMask.Add(false);
        return new(parsed.Speakers, [.. ids], [.. speechMask]);
    }

    private void EncodeWord(string word, ICollection<int> result)
    {
        if (word.Length == 0)
        {
            return;
        }
        string normalized = word.Normalize(NormalizationForm.FormC);
        byte[] bytes = Encoding.UTF8.GetBytes(normalized);
        var symbols = bytes.Select(value => ByteAlphabet[value].ToString()).ToList();
        while (symbols.Count > 1)
        {
            int bestIndex = -1;
            int bestRank = int.MaxValue;
            for (int index = 0; index < symbols.Count - 1; index++)
            {
                string pair = symbols[index] + " " + symbols[index + 1];
                if (_mergeRanks.TryGetValue(pair, out int rank) && rank < bestRank)
                {
                    bestRank = rank;
                    bestIndex = index;
                }
            }
            if (bestIndex < 0)
            {
                break;
            }
            symbols[bestIndex] += symbols[bestIndex + 1];
            symbols.RemoveAt(bestIndex + 1);
        }
        foreach (string symbol in symbols)
        {
            result.Add(_vocabulary.TryGetValue(symbol, out int id) ? id : _unknownId);
        }
    }

    private void ValidateConfig(JsonElement config)
    {
        string? eos = config.TryGetProperty("eos_token", out JsonElement value)
            ? value.GetString()
            : null;
        if (!string.Equals(eos, "<|endoftext|>", StringComparison.Ordinal))
        {
            throw new InvalidDataException("tokenizer_config.json does not select VibeVoice EOS.");
        }
    }

    private static void AppendEncoded(
        ICollection<int> ids,
        ICollection<bool> speechMask,
        IReadOnlyList<int> tokens)
    {
        foreach (int token in tokens)
        {
            ids.Add(token);
            speechMask.Add(false);
        }
    }

    private static Dictionary<string, int> ReadVocabulary(JsonElement element)
    {
        var result = new Dictionary<string, int>(StringComparer.Ordinal);
        foreach (JsonProperty property in element.EnumerateObject())
        {
            result.Add(property.Name, property.Value.GetInt32());
        }
        return result;
    }

    private static int SpecialId(IReadOnlyDictionary<string, int> special, string token) =>
        special.TryGetValue(token, out int id)
            ? id
            : throw new InvalidDataException($"Tokenizer is missing special token '{token}'.");

    private static JsonElement Required(JsonElement parent, string property) =>
        parent.TryGetProperty(property, out JsonElement value)
            ? value
            : throw new InvalidDataException($"Tokenizer JSON is missing '{property}'.");

    private static char[] CreateByteAlphabet()
    {
        var visible = new List<int>();
        visible.AddRange(Enumerable.Range('!', '~' - '!' + 1));
        visible.AddRange(Enumerable.Range('¡', '¬' - '¡' + 1));
        visible.AddRange(Enumerable.Range('®', 'ÿ' - '®' + 1));
        var bytes = visible.ToHashSet();
        var map = new char[256];
        int replacement = 256;
        foreach (int value in visible)
        {
            map[value] = (char)value;
        }
        for (int value = 0; value < 256; value++)
        {
            if (!bytes.Contains(value))
            {
                map[value] = (char)replacement++;
            }
        }
        return map;
    }
}

public sealed record VibeVoiceSpeakerLine(int Speaker, string Text);

public sealed record VibeVoiceSpeakerScript(IReadOnlyList<VibeVoiceSpeakerLine> Speakers)
{
    public static VibeVoiceSpeakerScript Parse(string script)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(script);
        var result = new List<VibeVoiceSpeakerLine>();
        foreach (string rawLine in script.Split(
                     ["\r\n", "\n"],
                     StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries))
        {
            Match match = Regex.Match(
                rawLine,
                @"^Speaker\s+([1-4])\s*:\s*(\S.*)$",
                RegexOptions.IgnoreCase | RegexOptions.CultureInvariant);
            if (!match.Success ||
                !int.TryParse(match.Groups[1].Value, out int speaker) ||
                speaker is < 1 or > 4)
            {
                throw new ArgumentException(
                    "Every script line must use 'Speaker N: text' with N from 1 through 4.");
            }
            result.Add(new(speaker, match.Groups[2].Value.Trim()));
        }
        if (result.Count == 0)
        {
            throw new ArgumentException("VibeVoice script must contain at least one speaker line.");
        }
        int[] speakers = result.Select(line => line.Speaker).Distinct().Order().ToArray();
        if (!speakers.SequenceEqual(Enumerable.Range(1, speakers.Length)))
        {
            throw new ArgumentException(
                "VibeVoice scripts must use contiguous Speaker 1..N identifiers.");
        }
        return new(result);
    }
}

public sealed record VibeVoicePrompt(
    IReadOnlyList<VibeVoiceSpeakerLine> Speakers,
    IReadOnlyList<int> InputIds,
    IReadOnlyList<bool> SpeechInputMask);
