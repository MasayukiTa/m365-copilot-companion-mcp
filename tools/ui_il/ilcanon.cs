// ilcanon.cs -- dump every method body of a .NET Framework assembly as normalised IL text, one line per method,
// sorted, so two builds of the same program can be compared after a pure move of code between files.
// Normalised: metadata tokens are resolved to names (so token renumbering does not matter), compiler-generated
// counters (<>c__DisplayClassN, b__N, <>9__N, CS$<>8__lockN ...) are replaced by '#'.
// Usage: ilcanon.exe in.exe out.txt
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Reflection.Emit;
using System.Text;
using System.Text.RegularExpressions;

static class IlCanon
{
    static Dictionary<short, OpCode> ops = new Dictionary<short, OpCode>();
    static Regex gen1 = new Regex(@"<>c__DisplayClass[0-9a-fA-F]+(_[0-9a-fA-F]+)?");
    static Regex gen2 = new Regex(@"b__\w+");
    static Regex gen3 = new Regex(@"CS\$<>9__CachedAnonymousMethodDelegate[0-9a-fA-F]+|<>9__\d+|<>8__[0-9a-fA-F]+|CS\$<>8__locals[0-9a-fA-F]+|<>f__am\$cache\w+|<>f__mg\$cache\w+|<>c__[0-9a-fA-F]+|CachedAnonymousMethodDelegate[0-9a-fA-F]+");
    static Regex gen4 = new Regex(@"\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}");
    static Regex gen5 = new Regex(@"\$\$method0x[0-9A-Fa-f]+");
    static string N(string s) { s = gen1.Replace(s, "<>c__DisplayClass#"); s = gen2.Replace(s, "b__#"); s = gen4.Replace(s, "{GUID}"); s = gen5.Replace(s, "$$method#"); return gen3.Replace(s, "<>gen#"); }

    static string Sig(MethodBase m)
    {
        var sb = new StringBuilder();
        sb.Append(N(m.DeclaringType.FullName)).Append("::").Append(N(m.Name)).Append('(');
        sb.Append(string.Join(",", m.GetParameters().Select(p => N(p.ParameterType.ToString()) + (p.IsOut ? "&out" : ""))));
        sb.Append(')');
        var mi = m as MethodInfo; if (mi != null) sb.Append("->").Append(N(mi.ReturnType.ToString()));
        sb.Append(" [").Append(m.Attributes).Append(']');
        return sb.ToString();
    }

    static string Tok(Module mod, int tok, Type[] ta, Type[] ma)
    {
        try
        {
            int table = tok >> 24;
            if (table == 0x70) return "\"" + mod.ResolveString(tok).Replace("\n", "\\n").Replace("\r", "\\r") + "\"";
            if (table == 0x11) return "sig";
            var m = mod.ResolveMember(tok, ta, ma);
            var mb = m as MethodBase;
            if (mb != null) return "M:" + Sig(mb);
            var f = m as FieldInfo; if (f != null) return "F:" + N(f.DeclaringType.FullName) + "::" + N(f.Name) + ":" + N(f.FieldType.ToString());
            var t = m as Type; if (t != null) return "T:" + N(t.ToString());
            return "?" + N(m.ToString());
        }
        catch (Exception e) { return "unresolved:" + e.GetType().Name; }
    }

    static string Body(MethodBase m)
    {
        MethodBody mb = m.GetMethodBody();
        if (mb == null) return "<no body>";
        byte[] il = mb.GetILAsByteArray();
        var sb = new StringBuilder();
        Type[] ta = m.DeclaringType.IsGenericType ? m.DeclaringType.GetGenericArguments() : null;
        Type[] ma = (m is MethodInfo && m.IsGenericMethod) ? m.GetGenericArguments() : null;
        sb.Append("locals=").Append(string.Join(",", mb.LocalVariables.Select(l => N(l.LocalType.ToString())))).Append(";init=").Append(mb.InitLocals).Append(";");
        int i = 0;
        while (i < il.Length)
        {
            OpCode op;
            if (il[i] == 0xFE) { op = ops[(short)(0xFE00 | il[i + 1])]; i += 2; }
            else { op = ops[il[i]]; i += 1; }
            sb.Append(op.Name);
            switch (op.OperandType)
            {
                case OperandType.InlineNone: break;
                case OperandType.ShortInlineBrTarget: sb.Append(' ').Append((sbyte)il[i]); i += 1; break;
                case OperandType.ShortInlineI: sb.Append(' ').Append((sbyte)il[i]); i += 1; break;
                case OperandType.ShortInlineVar: sb.Append(' ').Append(il[i]); i += 1; break;
                case OperandType.InlineVar: sb.Append(' ').Append(BitConverter.ToInt16(il, i)); i += 2; break;
                case OperandType.InlineBrTarget: sb.Append(' ').Append(BitConverter.ToInt32(il, i)); i += 4; break;
                case OperandType.InlineI: sb.Append(' ').Append(BitConverter.ToInt32(il, i)); i += 4; break;
                case OperandType.ShortInlineR: sb.Append(' ').Append(BitConverter.ToSingle(il, i)); i += 4; break;
                case OperandType.InlineI8: sb.Append(' ').Append(BitConverter.ToInt64(il, i)); i += 8; break;
                case OperandType.InlineR: sb.Append(' ').Append(BitConverter.ToDouble(il, i)); i += 8; break;
                case OperandType.InlineSwitch:
                    int n = BitConverter.ToInt32(il, i); i += 4; sb.Append(" sw").Append(n);
                    for (int k = 0; k < n; k++) { sb.Append(',').Append(BitConverter.ToInt32(il, i)); i += 4; }
                    break;
                default: // Field, Method, Sig, String, Tok, Type
                    sb.Append(' ').Append(Tok(m.Module, BitConverter.ToInt32(il, i), ta, ma)); i += 4; break;
            }
            sb.Append(';');
        }
        foreach (var c in mb.ExceptionHandlingClauses)
            sb.Append("EH:").Append(c.Flags).Append(',').Append(c.TryOffset).Append(',').Append(c.TryLength).Append(',').Append(c.HandlerOffset).Append(',').Append(c.HandlerLength)
              .Append(',').Append(c.Flags == ExceptionHandlingClauseOptions.Clause ? N(c.CatchType.ToString()) : "").Append(';');
        return sb.ToString();
    }

    static void Main(string[] a)
    {
        foreach (var f in typeof(OpCodes).GetFields(BindingFlags.Public | BindingFlags.Static))
        { var o = (OpCode)f.GetValue(null); ops[o.Value] = o; }
        var asm = Assembly.LoadFrom(a[0]);
        var lines = new List<string>();
        BindingFlags all = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.Static | BindingFlags.DeclaredOnly;
        foreach (var t in asm.GetTypes())
        {
            lines.Add("TYPE " + N(t.FullName) + " : " + N(t.BaseType == null ? "" : t.BaseType.ToString()) + " [" + t.Attributes + "] ifaces=" + string.Join(",", t.GetInterfaces().Select(x => x.ToString()).OrderBy(x => x)));
            foreach (var fld in t.GetFields(all).OrderBy(x => N(x.Name)))
                lines.Add("FIELD " + N(t.FullName) + "::" + N(fld.Name) + ":" + N(fld.FieldType.ToString()) + " [" + fld.Attributes + "]");
            foreach (var m in t.GetConstructors(all).Cast<MethodBase>().Concat(t.GetMethods(all)))
            {
                string body;
                try { body = Body(m); } catch (Exception e) { body = "ERR " + e.GetType().Name + e.Message; }
                lines.Add("METHOD " + Sig(m) + " | " + body);
            }
        }
        lines.Sort(StringComparer.Ordinal);
        File.WriteAllLines(a[1], lines.ToArray(), new UTF8Encoding(false));
        Console.WriteLine("types/lines: " + lines.Count);
    }
}
