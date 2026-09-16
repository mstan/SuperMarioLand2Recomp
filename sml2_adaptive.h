#pragma once
#include <stdint.h>
struct GBContext;
void sml2_adaptive_init(struct GBContext *ctx);
int sml2_adaptive_debug(const char *cmd, int id, const char *json);
