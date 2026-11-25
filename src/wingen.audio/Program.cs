using System.Diagnostics;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.Mxc.Sdk;
using Microsoft.Mxc.Sdk.V1;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using PowerTorch;
using PowerTorch.Cpu;
using PowerTorch.DirectML;
using WingenAudio.VibeVoice;
using WingenAudio.Contracts;

return await WingenAudioHost.RunAsync(args);

internal static class WingenAudioHost
{
    private static readonly JsonSerializerOptions JsonOptions = new(JsonSerializerDefaults.Web)
    {
        WriteIndented = false,
    };

    public static async Task<int> RunAsync(string[] args)
    {
        try
        {
            if (args.Length == 0 || args is ["help"] or ["--help"] or ["-h"])
            {
                PrintHelp();
                return 0;
            }

            return args[0] switch
            {
                "version" => WriteVersion(),
                "cube-layers" => RunCubeLayers(args[1..]),
                "mxc" => await RunMxcAsync(args[1..]),
                "powertorch" => RunPowerTorch(args[1..]),
                "vibevoice" => RunVibeVoice(args[1..]),
                "worker" => await RunWorkerAsync(args[1..]),
                "worker-server" => await RunWorkerServerAsync(args[1..]),
                "score" => RunScore(args[1..]),
                _ => throw new ArgumentException($"Unknown wingen.audio command '{args[0]}'."),
            };
        }
        catch (Exception error) when (error is ArgumentException or JsonException or IOException or HttpRequestException or MxcException)
        {
            Console.Error.WriteLine(JsonSerializer.Serialize(new
            {
                ok = false,
                error = error.GetType().Name,
                message = error.Message,
            }, JsonOptions));
            return 1;
        }
    }

    private static int WriteVersion()
    {
        Console.WriteLine(JsonSerializer.Serialize(new
        {
            ok = true,
            product = "wingen.audio",
            version = typeof(WingenAudioHost).Assembly.GetName().Version?.ToString(),
            runtime = Environment.Version.ToString(),
            platform = Environment.OSVersion.VersionString,
            mxc = MxcPlatform.NativeVersion,
        }, JsonOptions));
        return 0;
    }

    private static int RunCubeLayers(string[] args)
    {
        var options = ParseOptions(args);
        var result = CubeLayers.Generate(
            Require(options, "input"),
            Require(options, "output"),
            options.GetValueOrDefault("png") ?? string.Empty,
            Require(options, "stem"),
            Require(options, "engine"),
            options.GetValueOrDefault("label") ?? string.Empty,
            options.GetValueOrDefault("repo-root") ?? Directory.GetCurrentDirectory());
        Console.WriteLine(JsonSerializer.Serialize(new
        {
            ok = true,
            output = result.OutputPath,
            png = result.PngPath?.Value,
            result.SourceSha256,
            pointCount = result.PointCount,
            cubeShape = new[] { result.CubeShape.Item1, result.CubeShape.Item2 },
            durationSeconds = result.DurationSeconds,
            invHdr = result.InvHdr,
        }, JsonOptions));
        return 0;
    }

    private static async Task<int> RunWorkerServerAsync(string[] args)
    {
        var options = ParseOptions(args);
        var engine = WorkerDomain.ParseEngine(Require(options, "engine"));
        var listen = new Uri(Require(options, "listen"));
        var upstream = new Uri(Require(options, "upstream").TrimEnd('/') + "/");
        if (!IsLoopback(listen) || !IsLoopback(upstream))
        {
            throw new ArgumentException("worker-server listen and upstream URIs must be loopback-only.");
        }

        var builder = WebApplication.CreateSlimBuilder();
        builder.Logging.ClearProviders();
        builder.Logging.AddSimpleConsole(logging =>
        {
            logging.SingleLine = true;
            logging.TimestampFormat = "O ";
        });
        builder.WebHost.UseUrls(listen.GetLeftPart(UriPartial.Authority));
        builder.Services.AddHttpClient("wingen-worker", client =>
        {
            client.BaseAddress = upstream;
            client.Timeout = TimeSpan.FromSeconds(GetInt(options, "timeout", 1800));
        });
        await using var app = builder.Build();
        app.Use(async (context, next) =>
        {
            context.Response.Headers["X-Wingen-Audio-Control"] = "native";
            await next();
        });

        app.MapMethods("/{**workerPath}", new[] { "GET", "POST" }, async (
            HttpContext context,
            IHttpClientFactory factory,
            CancellationToken cancellationToken) =>
        {
            var path = context.Request.Path.Value ?? "/";
            if (!path.Equals("/", StringComparison.Ordinal)
                && !path.Equals("/status", StringComparison.Ordinal)
                && !path.Equals("/load", StringComparison.Ordinal)
                && !path.Equals("/unload", StringComparison.Ordinal)
                && !path.Equals("/generate", StringComparison.Ordinal)
                && !path.StartsWith("/v1/", StringComparison.Ordinal))
            {
                context.Response.StatusCode = StatusCodes.Status404NotFound;
                await context.Response.WriteAsJsonAsync(new
                {
                    ok = false,
                    synthesizedSpeech = false,
                    errors = new[] { new { code = "not_found", message = "Unknown worker route." } },
                }, cancellationToken);
                return;
            }

            await using var requestBuffer = new MemoryStream();
            if (context.Request.ContentLength is > 64 * 1024)
            {
                context.Response.StatusCode = StatusCodes.Status413PayloadTooLarge;
                await context.Response.WriteAsJsonAsync(new
                {
                    ok = false,
                    errors = new[] { new { code = "resource_limit", message = "JSON request exceeds 64 KiB." } },
                }, cancellationToken);
                return;
            }
            await context.Request.Body.CopyToAsync(requestBuffer, cancellationToken);
            if (requestBuffer.Length > 64 * 1024)
            {
                context.Response.StatusCode = StatusCodes.Status413PayloadTooLarge;
                return;
            }

            var upstreamRequest = new HttpRequestMessage(
                new HttpMethod(context.Request.Method),
                path + context.Request.QueryString);
            if (requestBuffer.Length > 0)
            {
                upstreamRequest.Content = new ByteArrayContent(requestBuffer.ToArray());
                if (context.Request.ContentType is not null)
                {
                    upstreamRequest.Content.Headers.ContentType = MediaTypeHeaderValue.Parse(context.Request.ContentType);
                }
            }
            using var upstreamResponse = await factory.CreateClient("wingen-worker")
                .SendAsync(upstreamRequest, HttpCompletionOption.ResponseHeadersRead, cancellationToken);
            await using var upstreamStream = await upstreamResponse.Content.ReadAsStreamAsync(cancellationToken);
            await using var responseBuffer = new MemoryStream();
            var responseChunk = new byte[65536];
            int responseRead;
            var responseTotal = 0;
            while ((responseRead = await upstreamStream.ReadAsync(responseChunk, cancellationToken)) > 0)
            {
                responseTotal += responseRead;
                if (responseTotal > 4 * 1024 * 1024)
                {
                    throw new IOException("Worker backend response exceeds 4 MiB.");
                }
                await responseBuffer.WriteAsync(responseChunk.AsMemory(0, responseRead), cancellationToken);
            }

            context.Response.StatusCode = (int)upstreamResponse.StatusCode;
            context.Response.ContentType = "application/json; charset=utf-8";
            if (path is "/" or "/status" or "/v1/status")
            {
                var node = JsonNode.Parse(responseBuffer.ToArray()) as JsonObject
                    ?? throw new JsonException("Worker status returned non-object JSON.");
                node["controlMode"] = "wingen.audio/mxc";
                node["controlPid"] = Environment.ProcessId;
                node["controlEngine"] = WorkerDomain.EngineId(engine);
                await context.Response.WriteAsync(node.ToJsonString(JsonOptions), cancellationToken);
                return;
            }
            await context.Response.Body.WriteAsync(responseBuffer.ToArray(), cancellationToken);
        });

        app.Lifetime.ApplicationStarted.Register(() =>
            Console.Error.WriteLine($"wingen.audio worker-server engine={WorkerDomain.EngineId(engine)} listen={listen} upstream={upstream} pid={Environment.ProcessId}"));
        await app.RunAsync();
        return 0;
    }

    private static async Task<int> RunWorkerAsync(string[] args)
    {
        var options = ParseOptions(args);
        var action = WorkerActionDomain.ParseAction(Require(options, "action"));
        var engine = WorkerDomain.ParseEngine(Require(options, "engine"));
        var url = options.GetValueOrDefault("url")
            ?? DefaultWorkerUrl(engine);
        var timeoutSeconds = GetInt(options, "timeout", 1200);
        var route = WorkerActionDomain.MethodAndPath(action);
        var method = route.Item1;
        var path = route.Item2;
        JsonObject? request = null;
        if (action == WorkerAction.Generate)
        {
            request = await ReadObjectAsync(Require(options, "request"));
        }

        using var client = new HttpClient
        {
            Timeout = TimeSpan.FromSeconds(timeoutSeconds),
            BaseAddress = new Uri(url.TrimEnd('/') + "/"),
        };
        client.DefaultRequestHeaders.Accept.Add(new MediaTypeWithQualityHeaderValue("application/json"));
        using var message = new HttpRequestMessage(new HttpMethod(method), path.TrimStart('/'));
        if (method == "POST")
        {
            message.Content = new StringContent(
                request?.ToJsonString(JsonOptions) ?? "{}",
                Encoding.UTF8,
                "application/json");
        }

        using var response = await client.SendAsync(message, HttpCompletionOption.ResponseHeadersRead);
        await using var stream = await response.Content.ReadAsStreamAsync();
        using var buffer = new MemoryStream();
        var chunk = new byte[65536];
        var total = 0;
        int read;
        while ((read = await stream.ReadAsync(chunk)) > 0)
        {
            total += read;
            if (total > 4 * 1024 * 1024)
            {
                throw new IOException("Worker response exceeds 4 MiB.");
            }
            await buffer.WriteAsync(chunk.AsMemory(0, read));
        }

        var receipt = JsonNode.Parse(buffer.ToArray()) as JsonObject
            ?? throw new JsonException("Worker returned a non-object response.");
        ValidateReceipt(action, receipt);
        if (receipt["ok"]?.GetValue<bool>() == false)
        {
            Console.WriteLine(receipt.ToJsonString(JsonOptions));
            return 2;
        }
        if (action == WorkerAction.Generate && options.TryGetValue("output", out var output))
        {
            var source = Path.GetFullPath(receipt["output"]?.GetValue<string>()
                ?? throw new JsonException("Worker generation receipt omitted output."));
            if (!File.Exists(source))
            {
                throw new IOException($"Worker generation receipt names a missing output: {source}");
            }

            var destination = Path.GetFullPath(output);
            Directory.CreateDirectory(Path.GetDirectoryName(destination)!);
            var staging = destination + "." + Guid.NewGuid().ToString("N") + ".stage";
            try
            {
                File.Copy(source, staging, true);
                File.Move(staging, destination, true);
            }
            finally
            {
                if (File.Exists(staging))
                {
                    File.Delete(staging);
                }
            }
            receipt["output"] = destination;
        }

        Console.WriteLine(receipt.ToJsonString(JsonOptions));
        return receipt["ok"]?.GetValue<bool>() == false ? 2 : 0;
    }

    private static int RunPowerTorch(string[] args)
    {
        if (args.Length != 0 && args[0] != "probe")
        {
            throw new ArgumentException($"Unknown PowerTorch command '{args[0]}'.");
        }

        var cpu = new CpuBackend().GetCapabilities();
        using var directMl = DirectMLBackend.Create();
        directMl.SelfTest();
        var accelerator = directMl.GetCapabilities();
        var cuda = new PowerTorch.BackendCapability(
            Device.Cuda(),
            "PowerTorch.Cuda",
            "native CUDA backend not implemented",
            false,
            [],
            new Dictionary<string, bool>());
        Console.WriteLine(JsonSerializer.Serialize(new
        {
            ok = cpu.IsAvailable && accelerator.IsAvailable,
            runtime = Environment.Version.ToString(),
            pin = "89ed20f04a1572b926637c435471ede084940bb9",
            cpu,
            directMl = accelerator,
            cuda,
        }, JsonOptions));
        return cpu.IsAvailable && accelerator.IsAvailable ? 0 : 2;
    }

    private static int RunVibeVoice(string[] args)
    {
        if (args.Length == 0)
        {
            throw new ArgumentException("vibevoice requires capability or synthetic.");
        }

        if (args[0] == "synthetic")
        {
            var syntheticOptions = ParseOptions(args[1..]);
            var receipt = VibeVoiceRuntime.RunSyntheticValidation(
                syntheticOptions.GetValueOrDefault("tokenizer-dir"));
            Console.WriteLine(JsonSerializer.Serialize(receipt, JsonOptions));
            return receipt.Ok ? 0 : 2;
        }

        if (args[0] != "capability")
        {
            throw new ArgumentException($"Unknown vibevoice command '{args[0]}'.");
        }
        var options = ParseOptions(args[1..]);
        var receiptCapability = VibeVoiceRuntime.Inspect(
            Require(options, "model-dir"),
            options.GetValueOrDefault("tokenizer-dir"),
            options.GetValueOrDefault("script"),
            options.GetValueOrDefault("voice-root"),
            options.TryGetValue("voice-refs", out string? voiceRefs)
                ? voiceRefs.Split(';', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
                : null);
        Console.WriteLine(JsonSerializer.Serialize(receiptCapability, JsonOptions));
        return receiptCapability.GenerationReady ? 0 : 2;
    }

    private static async Task<int> RunMxcAsync(string[] args)
    {
        if (args.Length == 0)
        {
            throw new ArgumentException("mxc requires probe or run.");
        }

        if (args[0] == "probe")
        {
            var support = MxcPlatform.GetPlatformSupport();
            var available = MxcPlatform.GetAvailableBackends();
            Console.WriteLine(JsonSerializer.Serialize(new
            {
                ok = true,
                supported = support.IsSupported,
                reason = support.Reason,
                nativeVersion = MxcPlatform.NativeVersion,
                methods = support.AvailableMethods.Select(x => x.ToString().ToLowerInvariant()),
                backends = available.Select(x => new
                {
                    backend = x.Backend.ToString(),
                    tier = x.Tier?.ToString(),
                    capabilities = x.Capabilities.Select(y => y.ToString()),
                    x.Warnings,
                }),
            }, JsonOptions));
            return support.IsSupported ? 0 : 2;
        }

        if (args[0] != "run")
        {
            throw new ArgumentException($"Unknown mxc command '{args[0]}'.");
        }

        var options = ParseOptions(args[1..]);
        var command = Require(options, "command");
        var backend = options.GetValueOrDefault("backend") ?? "processcontainer";
        var timeoutMs = GetInt(options, "timeout-ms", 120_000);
        var request = new ContainerRequest(command)
        {
            TimeoutMs = (uint)timeoutMs,
            InheritDefaultEnvironment = true,
            Containment = CreateContainment(backend, options),
            Filesystem = CreateFilesystemPolicy(options),
            Network = CreateNetworkPolicy(options),
        };
        var result = await MxcContainer.RunAsync(request);
        Console.Out.Write(result.Stdout);
        Console.Error.Write(result.Stderr);
        Console.WriteLine(JsonSerializer.Serialize(new
        {
            ok = !result.TimedOut && result.ExitCode == 0,
            exitCode = result.ExitCode,
            timedOut = result.TimedOut,
            warnings = result.Warnings,
        }, JsonOptions));
        return result.TimedOut ? 124 : result.ExitCode;
    }

    private static int RunScore(string[] args)
    {
        var options = ParseOptions(args);
        var engine = WorkerDomain.ParseEngine(Require(options, "engine"));
        var signals = new PlacementSignals(
            GetBool(options, "ok", true),
            GetBool(options, "output-writable", true),
            GetBool(options, "root-exists", true),
            GetBool(options, "model-exists", true),
            GetBool(options, "loaded"),
            GetBool(options, "worker-online"),
            GetDouble(options, "free-memory-gib"),
            GetInt(options, "cpu-cores"),
            GetDouble(options, "gpu-total-mib"),
            GetDouble(options, "gpu-free-mib"),
            GetDouble(options, "gpu-utilization"),
            GetBool(options, "native-host"),
            GetDouble(options, "weight", 1));
        Console.WriteLine(JsonSerializer.Serialize(new
        {
            ok = true,
            engine = WorkerDomain.EngineId(engine),
            score = Placement.Score(engine, signals),
        }, JsonOptions));
        return 0;
    }

    private static Containment CreateContainment(string backend, IReadOnlyDictionary<string, string> options) =>
        backend.ToLowerInvariant() switch
        {
            "process" => new Containment.Process(),
            "processcontainer" => new Containment.ProcessContainer(),
            "wslc" => new Containment.Wslc
            {
                Image = options.GetValueOrDefault("image") ?? "ubuntu:latest",
                CpuCount = options.TryGetValue("cpu", out var cpu) ? uint.Parse(cpu) : null,
                MemoryMb = options.TryGetValue("memory-mb", out var memory) ? ulong.Parse(memory) : null,
                Gpu = options.GetValueOrDefault("gpu") == "true",
                StoragePath = options.GetValueOrDefault("storage"),
            },
            _ => throw new ArgumentException($"Unsupported MXC backend '{backend}'."),
        };

    private static FilesystemPolicy CreateFilesystemPolicy(IReadOnlyDictionary<string, string> options)
    {
        var policy = new FilesystemPolicy();
        if (options.TryGetValue("readwrite", out var readwrite))
        {
            policy.ReadwritePaths.AddRange(readwrite.Split(';', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries));
        }
        if (options.TryGetValue("readonly", out var readOnly))
        {
            policy.ReadonlyPaths.AddRange(readOnly.Split(';', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries));
        }
        if (options.TryGetValue("deny", out var denied))
        {
            policy.DeniedPaths.AddRange(denied.Split(';', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries));
        }
        return policy;
    }

    private static NetworkPolicy CreateNetworkPolicy(IReadOnlyDictionary<string, string> options) => new()
    {
        Egress = new NetworkEgressPolicy
        {
            Default = options.GetValueOrDefault("network-egress") == "allow" ? NetworkAction.Allow : NetworkAction.Deny,
        },
        Ingress = new NetworkIngressPolicy
        {
            Default = NetworkAction.Deny,
            HostLoopback = options.GetValueOrDefault("host-loopback") == "allow" ? NetworkAction.Allow : NetworkAction.Deny,
        },
    };

    private static void ValidateReceipt(WorkerAction action, JsonObject receipt)
    {
        if (receipt["ok"]?.GetValue<bool>() == false)
        {
            return;
        }
        if (action == WorkerAction.Load && receipt["loaded"]?.GetValue<bool>() != true)
        {
            throw new InvalidDataException("Worker load receipt does not report loaded=true.");
        }
        if (action == WorkerAction.Preflight && receipt["ready"]?.GetValue<bool>() != true)
        {
            throw new InvalidDataException("Worker preflight receipt does not report ready=true.");
        }
        if (action == WorkerAction.Generate &&
            (receipt["synthesizedSpeech"]?.GetValue<bool>() != true || string.IsNullOrWhiteSpace(receipt["output"]?.GetValue<string>())))
        {
            throw new InvalidDataException("Worker generation receipt is incomplete.");
        }
    }

    private static string DefaultWorkerUrl(WorkerEngine engine) =>
        $"http://127.0.0.1:{WorkerDomain.EnginePort(engine)}";

    private static async Task<JsonObject> ReadObjectAsync(string path)
    {
        await using var stream = File.OpenRead(path);
        var node = await JsonNode.ParseAsync(stream, documentOptions: new JsonDocumentOptions { MaxDepth = 32 }) as JsonObject
            ?? throw new JsonException("Request file must contain a JSON object.");
        return node;
    }

    private static Dictionary<string, string> ParseOptions(string[] args)
    {
        var result = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        for (var index = 0; index < args.Length; index++)
        {
            var current = args[index];
            if (!current.StartsWith("--", StringComparison.Ordinal))
            {
                throw new ArgumentException($"Expected option name, got '{current}'.");
            }
            var name = current[2..];
            if (index + 1 >= args.Length || args[index + 1].StartsWith("--", StringComparison.Ordinal))
            {
                result[name] = "true";
            }
            else
            {
                result[name] = args[++index];
            }
        }
        return result;
    }

    private static string Require(IReadOnlyDictionary<string, string> options, string name) =>
        options.TryGetValue(name, out var value) && !string.IsNullOrWhiteSpace(value)
            ? value
            : throw new ArgumentException($"Missing required option --{name}.");

    private static int GetInt(IReadOnlyDictionary<string, string> options, string name, int fallback = 0) =>
        options.TryGetValue(name, out var value) ? int.Parse(value) : fallback;

    private static double GetDouble(IReadOnlyDictionary<string, string> options, string name, double fallback = 0) =>
        options.TryGetValue(name, out var value) ? double.Parse(value) : fallback;

    private static bool GetBool(IReadOnlyDictionary<string, string> options, string name, bool fallback = false) =>
        options.TryGetValue(name, out var value) ? bool.Parse(value) : fallback;

    private static void PrintHelp()
    {
        Console.WriteLine("""
wingen.audio version
wingen.audio cube-layers --input WAV --output JSON --stem STEM --engine ENGINE [--label LABEL] [--png PNG] [--repo-root ROOT]
wingen.audio worker --action <status|preflight|load|unload|generate> --engine <vibevoice|personaplex> [--url URL] [--request FILE] [--output FILE] [--timeout SECONDS]
wingen.audio worker-server --engine ENGINE --listen http://127.0.0.1:PORT --upstream http://127.0.0.1:PORT [--timeout SECONDS]
wingen.audio mxc probe
wingen.audio powertorch probe
wingen.audio vibevoice capability --model-dir PATH [--tokenizer-dir PATH] [--script TEXT --voice-root PATH --voice-refs A;B]
wingen.audio vibevoice synthetic [--tokenizer-dir PATH]
wingen.audio mxc run --command COMMAND [--backend process|processcontainer|wslc] [--readonly P1;P2] [--readwrite P1;P2] [--deny P1;P2] [--host-loopback allow] [--network-egress allow] [--timeout-ms MS] [--gpu true]
wingen.audio score --engine ENGINE --free-memory-gib N [placement signals]
""");
    }

    private static bool IsLoopback(Uri uri) =>
        uri.IsLoopback && (uri.Scheme == Uri.UriSchemeHttp || uri.Scheme == Uri.UriSchemeHttps);
}
