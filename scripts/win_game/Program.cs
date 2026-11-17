// Dynamic win harness for "IDP - Sorteios Especiais" (idp-ctf-03-sorteio).
//
// Runs the REAL game logic (IdpSorteios.Core.dll) with a controlled clock:
//   seed = unix seconds  →  Random(seed).Next() % 1000 + 1  →  ball 42 wins
// It finds the next second T whose ball is 42, then executes the genuine
// Competition.Start() → TeamRound.Draw() at T via reflection and prints the
// flag the winner sees.
//
// Build with SDK 8, run with the installed .NET 10 runtime (RollForward
// LatestMajor) so System.Random matches the game's runtime exactly.

using System.Reflection;
using System.Text;

Console.OutputEncoding = Encoding.UTF8;

var gameDir = @"C:\Users\Administrator\AppData\Local\Programs\IDP Sorteios Especiais";
var appAsm = Assembly.LoadFrom(Path.Combine(gameDir, "IdpSorteiosEspeciais.dll"));
var coreAsm = Assembly.LoadFrom(Path.Combine(gameDir, "IdpSorteios.Core.dll"));
var infraAsm = Assembly.LoadFrom(Path.Combine(gameDir, "IdpSorteios.Infrastructure.dll"));

const BindingFlags Any = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.Static;

// ---- 1) embedded teams (times.csv manifest resource) -----------------------
using var csv = (Stream)appAsm.GetManifestResourceStream("times.csv")!;
var teamCsvType = infraAsm.GetType("IdpSorteios.Infrastructure.TeamCsv")!;
var readAsync = teamCsvType.GetMethod("ReadAsync")!;
var task = (Task)readAsync.Invoke(null, new object[] { csv, CancellationToken.None })!;
await task;
var teamsObj = task.GetType().GetProperty("Result")!.GetValue(task)!;

var teamType = coreAsm.GetType("IdpSorteios.Core.Team")!;
var nameProp = teamType.GetProperty("Name")!;
var flagProp = teamType.GetProperty("Flag")!;
Console.WriteLine("[csv] teams embedded (nome -> flag):");
foreach (var t in (System.Collections.IEnumerable)teamsObj)
    Console.WriteLine($"  {nameProp.GetValue(t)} -> {flagProp.GetValue(t)}");

// ---- 2) find the next second T whose ball is 42 ----------------------------
long now = DateTimeOffset.UtcNow.ToUnixTimeSeconds();
long T = -1;
for (long t = now + 1; t < now + 3600; t++)
{
    var n = new Random((int)t).Next() % 1000 + 1;
    if (n == 42) { T = t; break; }
}
if (T < 0) { Console.WriteLine("[rng] no winning second within 1h"); return 1; }
Console.WriteLine($"[rng] next winning second T = {T} ({T - now}s from now)");

// ---- 3) execute the genuine game logic at T --------------------------------
var atT = DateTimeOffset.FromUnixTimeSeconds(T);
var fakeTp = new FakeTimeProvider(atT);
var engineType = coreAsm.GetType("IdpSorteios.Core.DrawEngine")!;
var engine = Activator.CreateInstance(engineType, fakeTp)!;          // _random = Random((int)T)

var compType = coreAsm.GetType("IdpSorteios.Core.Competition")!;
var compCtor = compType.GetConstructors(Any).First();
Console.WriteLine($"[ctor] Competition({string.Join(", ", compCtor.GetParameters().Select(p => $"{p.ParameterType.Name} {p.Name}"))})");
var comp = compCtor.Invoke(new[] { teamsObj, engine, (object)fakeTp })!;

var teamsProp = compType.GetProperty("Teams")!;
var statuses = (System.Collections.IEnumerable)teamsProp.GetValue(comp)!;
var statusType = coreAsm.GetType("IdpSorteios.Core.TeamStatus")!;
var statusTeamProp = statusType.GetProperty("Team")!;
var statusList = statuses.Cast<object>().ToList();
var chosen = statusList[0];
Console.WriteLine($"[game] competition with {statusList.Count} rounds; playing as team '{nameProp.GetValue(statusTeamProp.GetValue(chosen))}'");

var start = compType.GetMethod("Start", Any)!;
object round;
try
{
    round = start.Invoke(comp, new[] { chosen })!;   // Reseed(engine, T) + new TeamRound(status, engine, tp)
}
catch (Exception ex)
{
    Console.WriteLine($"[err] Start failed: {ex.InnerException?.Message ?? ex.Message}");
    return 1;
}

var roundType = coreAsm.GetType("IdpSorteios.Core.TeamRound")!;
var outcomeType = coreAsm.GetType("IdpSorteios.Core.DrawOutcome")!;
var drawMethod = roundType.GetMethod("Draw", Any)!;
var callback = (Action<int>)(n => { });   // animation callback (GUI updates ball number)
try
{
var outcome = drawMethod.Invoke(round, new[] { callback })!;
Console.WriteLine($"[draw] ball={outcomeType.GetProperty("Number")!.GetValue(outcome)} " +
                  $"isWinner={outcomeType.GetProperty("IsWinner")!.GetValue(outcome)} " +
                  $"attemptsRemaining={outcomeType.GetProperty("AttemptsRemaining")!.GetValue(outcome)}");
var flag = outcomeType.GetProperty("Flag")!.GetValue(outcome);
Console.WriteLine($"[WIN] flag={flag}");
return 0;
}
catch (Exception ex)
{
    Console.WriteLine($"[err] draw failed: {ex.InnerException?.ToString() ?? ex.ToString()}");
    return 1;
}

static class Harness
{
    public static Func<int, T> BuildCallback<T>(ConstructorInfo ctor) where T : class
        => n => (T)ctor.Invoke(new object?[] { n, false, 3, null });
}

sealed class FakeTimeProvider(DateTimeOffset t) : TimeProvider
{
    public override DateTimeOffset GetUtcNow() => t;
}
