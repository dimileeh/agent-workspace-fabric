# AWF Core provenance carrier — empty image whose config labels bind
# build-ID → digest identity for awf-cloud consumers.
#
# Labels are supplied at `docker build` time via --label (not baked as floating
# defaults). See cloudbuild.yaml + scripts/cloudbuild_provenance.py.
FROM scratch
