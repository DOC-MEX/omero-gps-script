#!/usr/bin/env python3

import os
import shutil
import subprocess
import tempfile

import omero.scripts as scripts
from omero.gateway import BlitzGateway, MapAnnotationWrapper
from omero.rtypes import rstring, rlong, robject
from omero.sys import ParametersI


P_DTYPE = "Data_Type"
P_IDS = "IDs"
P_NAMESPACE = "Namespace"
P_CUSTOM_NAMESPACE = "Custom namespace"

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".tif", ".tiff", ".png"}

OMERO_GEO_NS = "openmicroscopy.org/omero/client/mapAnnotation/geolocation"
GEOSPARQL_NS = "http://www.opengis.net/ont/geosparql#"


def find_original_files(conn, image_id):
    """
    Find the original file associated with an OMERO Image.

    GPS metadata is stored in the original image file so we need to locate the OriginalFile.
    """
    query = """
        select f.id, f.name
        from Image i
        join i.fileset fs
        join fs.usedFiles uf
        join uf.originalFile f
        where i.id = :image_id
    """
    params = ParametersI()
    params.addLong("image_id", image_id)

    rows = conn.getQueryService().projection(query, params, conn.SERVICE_OPTS)
    return [(row[0].val, row[1].val) for row in rows]


def download_original_file(conn, original_file_id, filename):
    """
    Download the OMERO OriginalFile to a temporary local file.

    ExifTool works on local files, so each image is downloaded temporarily
    and deleted immediately after GPS extraction.
    """
    suffix = os.path.splitext(filename)[1]

    fd, path = tempfile.mkstemp(prefix="omero_original_", suffix=suffix)
    os.close(fd)

    raw_file_store = conn.c.sf.createRawFileStore()

    try:
        raw_file_store.setFileId(original_file_id)
        size = raw_file_store.size()

        offset = 0
        chunk_size = 1024 * 1024

        with open(path, "wb") as f:
            while offset < size:
                length = min(chunk_size, size - offset)
                data = raw_file_store.read(offset, length)
                f.write(data)
                offset += length

    finally:
        raw_file_store.close()

    return path


def exiftool_value(path, tag, numeric=False):
    """Read one EXIF tag from the temporary image file using ExifTool."""
    cmd = ["exiftool", "-s3"]

    if numeric:
        cmd.append("-n")

    cmd += [f"-{tag}", path]

    result = subprocess.run(cmd, text=True, capture_output=True)

    if result.returncode != 0:
        return ""

    output = result.stdout.strip()
    return output.splitlines()[0] if output else ""


def extract_gps(path):
    """
    Extract the basic GPS values used by the new Omero annotation.

    Latitude and longitude are required. Altitude is optional and set to
    NA if it is not available in the EXIF metadata.
    """
    lat = exiftool_value(path, "GPSLatitude", numeric=True)
    lon = exiftool_value(path, "GPSLongitude", numeric=True)

    if not lat or not lon:
        return None

    return {
        "latitude": lat,
        "longitude": lon,
        "altitude": exiftool_value(path, "GPSAltitude", numeric=True) or "NA",
    }


def osm_url(lat, lon):
    """Create an OpenStreetMap link from latitude and longitude."""
    return (
        "https://www.openstreetmap.org/"
        f"?mlat={lat}&mlon={lon}#map=16/{lat}/{lon}"
    )


def has_geolocation_annotation(image, namespace):
    """
    Check if the image already has a MapAnnotation in this namespace.
    
    """
    for ann in image.listAnnotations():
        if "MapAnnotation" not in type(ann).__name__:
            continue

        if ann.getNs() == namespace:
            return True

    return False


def add_geolocation_annotation(conn, image, gps, source_file, namespace):
    """
    Create and link one MapAnnotation containing the GPS values.

    The annotation uses a configurable namespace. By default this our
    OMERO geolocation namespace, but the user may choose GeoSPARQL or a
    custom namespace from the script form.
    """
    lat = gps["latitude"]
    lon = gps["longitude"]

    values = [
        ["latitude", lat],
        ["longitude", lon],
        ["altitude", gps["altitude"]],
        ["osm_url", osm_url(lat, lon)],
        ["source", "EXIF GPS"],
        ["source_file", source_file],
    ]

    ann = MapAnnotationWrapper(conn)
    ann.setNs(namespace)
    ann.setValue(values)
    ann.save()

    image.linkAnnotation(ann)

    return ann


def process_image(conn, image, namespace):
    """
    Process one Image.

    The function finds the original file, downloads it temporarily, extracts
    GPS metadata, creates the MapAnnotation, and removes the temporary file.
    """
    image_id = image.getId()

    if has_geolocation_annotation(image, namespace):
        return "already_annotated", None

    selected = None

    for file_id, file_name in find_original_files(conn, image_id):
        suffix = os.path.splitext(file_name)[1].lower()

        if suffix in IMAGE_EXTENSIONS:
            selected = (file_id, file_name)
            break

    if selected is None:
        return "no_original_file", None

    file_id, file_name = selected
    temp_path = download_original_file(conn, file_id, file_name)

    try:
        gps = extract_gps(temp_path)

        if gps is None:
            return "no_gps", None

        ann = add_geolocation_annotation(conn, image, gps, file_name, namespace)
        return "annotated", ann.getId()

    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def get_images_to_process(conn, data_type, ids):
    """
    Accept either Dataset IDs or Image IDs from the OMERO.web form.

    Dataset option processes all images in the selected Dataset.
    Image option processes only the selected Image IDs.
    """
    images = []

    for object_id in ids:
        obj = conn.getObject(data_type, object_id)

        if obj is None:
            continue

        if data_type == "Image":
            images.append(obj)

        elif data_type == "Dataset":
            images.extend(list(obj.listChildren()))

    return images


def get_namespace(client):
    """Resolve the selected namespace, including custom user input."""
    namespace = client.getInput(P_NAMESPACE, unwrap=True)
    custom_namespace = client.getInput(P_CUSTOM_NAMESPACE, unwrap=True)

    if namespace == "Custom":
        if not custom_namespace:
            raise ValueError(
                "Custom namespace selected but no custom namespace was provided."
            )
        return custom_namespace

    return namespace


def run_script():
    client = scripts.client(
        "GPS annotations",
        (
            "Extract GPS EXIF metadata from original files and add "
            "geolocation MapAnnotations directly to Images."
        ),

        scripts.String(
            P_DTYPE,
            optional=False,
            grouping="1",
            values=[rstring("Dataset"), rstring("Image")],
            default="Dataset",
            description="Process all Images in a Dataset or selected Images only.",
        ),

        scripts.List(
            P_IDS,
            optional=False,
            grouping="1.1",
            description="Dataset ID(s) or Image ID(s) to process.",
        ).ofType(rlong(0)),

        scripts.String(
            P_NAMESPACE,
            optional=False,
            grouping="2",
            default=OMERO_GEO_NS,
            values=[
                rstring(OMERO_GEO_NS),
                rstring(GEOSPARQL_NS),
                rstring("Custom"),
            ],
            description="Namespace used for the created MapAnnotations.",
        ),

        scripts.String(
            P_CUSTOM_NAMESPACE,
            optional=True,
            grouping="2.1",
            default="",
            description="Only used when Namespace is set to Custom.",
        ),

        authors=["Daniel Olvera"],
        institutions=["MPI-EvolBio"],
        contact="https://forum.image.sc/tag/omero",
        version="0.5.0",
    )

    try:
        data_type = client.getInput(P_DTYPE, unwrap=True)
        ids = client.getInput(P_IDS, unwrap=True)
        namespace = get_namespace(client)

        conn = BlitzGateway(client_obj=client)

        if shutil.which("exiftool") is None:
            client.setOutput("ERROR", rstring("exiftool is not available."))
            return

        images = get_images_to_process(conn, data_type, ids)

        annotated = 0
        already_annotated = 0
        without_gps = 0
        without_original = 0
        result_obj = None

        for image in images:
            status, detail = process_image(conn, image, namespace)

            if status == "annotated":
                annotated += 1
                if result_obj is None:
                    result_obj = image

            elif status == "already_annotated":
                already_annotated += 1

            elif status == "no_gps":
                without_gps += 1

            elif status == "no_original_file":
                without_original += 1

        message = (
            f"Input type: {data_type}\n"
            f"Input IDs: {ids}\n"
            f"Images processed: {len(images)}\n\n"
            f"Annotated: {annotated}\n"
            f"Already annotated: {already_annotated}\n"
            f"Without GPS: {without_gps}\n"
            f"Without supported original file: {without_original}\n\n"
            f"Namespace:\n{namespace}"
        )

        client.setOutput("Message", rstring(message))

        if result_obj is not None:
            client.setOutput("Result", robject(result_obj._obj))

    except Exception as err:
        client.setOutput("ERROR", rstring(str(err)))
        raise

    finally:
        client.closeSession()


if __name__ == "__main__":
    run_script()