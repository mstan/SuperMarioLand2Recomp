#pragma once
#include <stdint.h>
struct GBContext;
void sml2_adaptive_init(struct GBContext *ctx);
int sml2_adaptive_debug(const char *cmd, int id, const char *json);

/* Why the wide margins cannot be composed for a given recompiled body, or NULL
 * when they can. `body_id` is a GBBody::id (SML2_BODY_FAITHFUL / SML2_BODY_DX);
 * pass NULL for the body that actually booted. The launcher shows this on the
 * Adaptive widescreen feature before either body exists, so it has to be
 * answerable for a body that is only a pending choice. */
const char *sml2_adaptive_margin_note(const char *body_id);
