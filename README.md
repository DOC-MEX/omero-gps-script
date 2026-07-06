# omero-gps-scripts

A collection of OMERO scripts for creating geolocation MapAnnotations from image GPS metadata.

Many digital cameras saved GPS coordinates directly in the image EXIF metadata, while others store GPS positions in separate GPS logging files.
These scripts automate the extraction of that information and store it as standard OMERO MapAnnotations.

The resulting annotations can later be exposed through RDF mappings and queried using GeoSPARQL engines such as QLever.

---

## Current scripts

### GPS_annotations.py

Extracts GPS metadata directly from the original image files already stored in OMERO.
