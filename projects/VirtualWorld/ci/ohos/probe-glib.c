/*
 * probe-glib.c — OpenHarmony/aarch64 cross-compile probe for GLib-2.0.
 *
 * Goal: prove that the project-private dependency prefix (build-qemu-ohos-deps/
 * prefix) yields a *real* aarch64-linux-ohos ELF that (a) includes <glib.h>,
 * (b) is linked against libglib-2.0 (and transitively libffi / libpcre2-8 /
 * libintl), and (c) calls actual GLib APIs. The binary is NOT executed on the
 * macOS host (it is an aarch64-OHOS musl binary); we only compile, link, and
 * inspect it with file/llvm-readelf. This is a build/link proof, not a runtime
 * proof — running it would require an OHOS device/emulator, which is out of
 * scope for the dependency layer.
 */
#include <glib.h>
#include <stdio.h>

int main(void)
{
    /* A trivial GLib string API. */
    gchar *greeting = g_strdup_printf("glib-2.0 probe %d", 1);

    /* A more substantial GLib container API (hash table). */
    GHashTable *ht = g_hash_table_new_full(g_str_hash, g_str_equal,
                                           g_free, g_free);
    g_hash_table_insert(ht, g_strdup("answer"), g_strdup("forty-two"));
    const gchar *v = (const gchar *) g_hash_table_lookup(ht, "answer");

    /* Version macros from glibconfig.h / glib.h. */
    printf("%s: lookup=%s glib=%d.%d.%d\n",
           greeting,
           v ? v : "(null)",
           glib_major_version, glib_minor_version, glib_micro_version);

    g_hash_table_destroy(ht);
    g_free(greeting);
    return 0;
}
