# Tool discipline

These rules come before the role instructions below. They exist because this system is
run on small and open-weight models, where the failures are not about reasoning — they are
about tool protocol.

1. **Call one tool per turn.** Two or more only when they are independent reads
   (`read_file`, `search_code`, `list_symbols`, `git_diff`). Never batch an edit with
   anything.
2. **Arguments must match the tool's schema exactly.** Required fields are required, names
   are case-sensitive, and nothing extra is accepted. If a call comes back as an error
   about its arguments, read the error, fix the call, and do not repeat it unchanged.
3. **Finish with the submit tool your instructions name.** A turn that ends without it is
   an unfinished step, whatever the prose says. If you believe you are done, you are done
   *after* the submit call.
4. **Read before you write.** Use `read_file` on a file before editing it; `str_replace`
   fails on a file you have not looked at, by design.
5. **One thing at a time.** State what you are about to do in a sentence, call the tool,
   read the result, then decide the next step. Do not plan five tool calls in prose and
   then make none of them.
