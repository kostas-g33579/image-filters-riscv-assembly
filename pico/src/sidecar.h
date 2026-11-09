/* sidecar.h - the JSON file that goes next to the sanitised NIfTI.
 *
 * Everything in it is one of: a constant, a label the user supplied (checked: 1..32 characters of
 * A-Z a-z 0-9 _ -), a number that dicom.c parsed and a range check accepted (printed exactly), a number
 * measured by geom.c (printed with 9 decimals), or a short text value that passed a strict character
 * check. Identifying tags appear only as a NAME and a COUNT ("PatientName": 192), never as a value.
 *
 * Blocks: "tool", "labels", "measured", "from_tags" (BIDS key names and units: seconds, degrees, tesla; the per-slice display window is deliberately not included),
 * "discrepancies" (where a tag and the measurement disagree, or a value was rejected: fixed wording, no values
 * copied from rejected text) and "audit".
 */
#ifndef SIDECAR_H
#define SIDECAR_H
#include <stdint.h>
#include "dicom.h"
#include "geom.h"
#include "nifti.h"

#define SC_LABEL_MAX 32
#define SC_MAX_JSON  8192

typedef struct {
    char subject[SC_LABEL_MAX + 1], sequence[SC_LABEL_MAX + 1], site[SC_LABEL_MAX + 1], scanner[SC_LABEL_MAX + 1];
} sc_labels;

/* 1 if s is 1..32 characters of A-Z a-z 0-9 _ - */
int sc_valid_label(const char *s);

typedef struct {
    dcm_info first;                         /* the first slice: the acquisition values come from it */
    uint32_t n;                             /* slices seen */
    uint32_t ident[DCM_N_IDENTIFYING];      /* slices in which each identifying field was present */
    uint32_t burned[4], removed[4];         /* BurnedInAnnotation / PatientIdentityRemoved: YES, NO, other, absent */
} sc_state;

void sc_init(sc_state *s);
void sc_add(sc_state *s, const dcm_info *t);        /* t must have passed dcm_check() */

/* Write the JSON into out (capacity cap). Returns its length, or -1 if it does not fit. */
int sc_build(const sc_state *s, const sc_labels *lab, const nii_state *ns, const geom_result *g, char *out, uint32_t cap);

#endif
