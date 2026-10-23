/*
 * captest_add.c — a tiny sibling shared object used by the dlopen capability
 * test. It exports a single auditable symbol `captest_add(int,int)` so the probe
 * can verify that dlopen/dlsym works in the target domain (shell or HAP).
 *
 * The symbol is intentionally trivial and verifiable: 2+3 must equal 5.
 */
int captest_add(int a, int b)
{
    return a + b;
}
