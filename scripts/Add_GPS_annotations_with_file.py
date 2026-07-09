#!/usr/bin/env python3
"""
Add geolocation MapAnnotations to OMERO Images using a GPS logging file.

The script matches the timestamp (DateTimeOriginal) stored in each image EXIF
metadata with the nearest GPS position recorded in an attached GPS LOG file.
The resulting latitude, longitude and matching provenance are stored as
standard OMERO MapAnnotations.
"""

import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta

import omero
import omero.scripts as scripts
from omero.gateway import BlitzGateway, MapAnnotationWrapper
from omero.rtypes import rstring, rlong, robject
from omero.sys import ParametersI


P_DTYPE = "Data_Type"
P_IDS = "IDs"
P_FILE_ANN = "File_Annotation"
P_NAMESPACE = "Namespace"
P_CUSTOM_NAMESPACE = "Custom namespace"

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".tif", ".tiff", ".png"}
# The parser has been tested with OM Systems  .LOG files.
# .NMEA and .TXT are accepted because they may contain the same NMEA records,
# but more GPS logger formats still need systematic testing.
LOG_EXTENSIONS = {".log", ".nmea", ".txt"}

# Fixed threshold used when matching image timestamps to GPS fixes.
MAX_TIME_DIFFERENCE_SECONDS = 45

OMERO_GEO_NS = "openmicroscopy.org/omero/client/mapAnnotation/geolocation"
GEOSPARQL_NS = "http://www.opengis.net/ont/geosparql#"


def find_original_files(conn, image_id):
    """
    Find the original file associated with an OMERO Image.

    DateTimeOriginal is stored in the original uploaded image file. 
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
    Download an OMERO OriginalFile to a temporary local file.

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


def get_latest_log_file_annotation(source_object):
    """
    Find the newest LOG-like FileAnnotation attached to the selected object.

    This follows the same the Import from CSV script.
    """
    selected = None

    for ann in source_object.listAnnotations():
        if ann.OMERO_TYPE != omero.model.FileAnnotationI:
            continue

        file_name = ann.getFile().getName()
        suffix = os.path.splitext(file_name)[1].lower()

        if suffix not in LOG_EXTENSIONS:
            continue

        if selected is None or ann.getDate() > selected.getDate():
            selected = ann

    if selected is None:
        raise RuntimeError(
            f"No GPS LOG FileAnnotation found on "
            f"{source_object.OMERO_CLASS}:{source_object.getId()}"
        )

    return selected


def get_log_file_annotation(conn, source_objects, file_ann_input):
    """
    Resolve the GPS LOG FileAnnotation used by the script.

    If the user selected a FileAnnotation in the script form, that file is used.
    If the field is left blank, the newest suitable LOG-like attachment on the
    first selected Dataset/Image is used.
    """
    if file_ann_input:
        file_ann_id = int(str(file_ann_input).split(",")[0])
        ann = conn.getObject("Annotation", file_ann_id)

        if ann is None:
            raise RuntimeError(f"FileAnnotation:{file_ann_id} not found")

        if ann.OMERO_TYPE != omero.model.FileAnnotationI:
            raise RuntimeError(
                f"Annotation:{file_ann_id} is not a FileAnnotation"
            )

        return ann

    if not source_objects:
        raise RuntimeError("No source object available to search for LOG file")

    return get_latest_log_file_annotation(source_objects[0])


def download_file_annotation(conn, file_ann):
    """
    Download the selected GPS LOG FileAnnotation.

    The file is written temporarily to disk so it can be parsed by the script.
    The temporary file is removed after the GPS fixes have been loaded.
    """
    original_file = file_ann.getFile()
    filename = original_file.getName()

    path = download_original_file(conn, original_file.getId(), filename)
    return path, filename


def exiftool_value(path, tag):
    """Read one EXIF tag from the temporary image file using ExifTool."""
    result = subprocess.run(
        ["exiftool", "-s3", f"-{tag}", path],
        text=True,
        capture_output=True,
    )

    if result.returncode != 0:
        return ""

    output = result.stdout.strip()
    return output.splitlines()[0] if output else ""


def get_image_datetime(path):
    """
    Read DateTimeOriginal from image EXIF metadata.

    DateTimeOriginal is normally stored as local camera time and usually has no
    timezone. The GPS LOG timestamps are converted to this local time before
    matching.
    """
    value = exiftool_value(path, "DateTimeOriginal")

    if not value:
        return None

    return datetime.strptime(value, "%Y:%m:%d %H:%M:%S")


def nmea_to_decimal(value, hemisphere):
    """Convert NMEA ddmm.mmmm / dddmm.mmmm coordinates to decimal degrees."""
    raw = float(value)
    degrees = int(raw // 100)
    minutes = raw - degrees * 100
    decimal = degrees + minutes / 60

    if hemisphere in ("S", "W"):
        decimal = -decimal

    return decimal


def parse_offset_from_header(line):
    """
    Parse the OM Digital LOG timezone offset.

    Example:
    @OM Digital Solutions/+0100/+0100

    The offset is applied to GPS UTC timestamps so they can be compared with
    the image DateTimeOriginal values recorded by the camera.
    """
    match = re.search(r"/([+-])(\d{2})(\d{2})", line)

    if not match:
        return timedelta(0)

    sign, hours, minutes = match.groups()
    offset = timedelta(hours=int(hours), minutes=int(minutes))

    if sign == "-":
        offset = -offset

    return offset


def parse_nmea_time(value):
    """Parse hhmmss.s NMEA time."""
    return value[0:2], value[2:4], value[4:6].split(".")[0]


def parse_nmea_date(value):
    """Parse ddmmyy NMEA date."""
    day = int(value[0:2])
    month = int(value[2:4])
    year = int(value[4:6]) + 2000
    return year, month, day


def load_gps_log(log_path):
    """
    Read GPS logs from an OM Digital / NMEA LOG file.

    The LOG file contains two complementary NMEA record types:
    - $GPRMC provides date, time, latitude and longitude.
    - $GPGGA provides altitude when available.

    GPS timestamps are stored in UTC. The timezone offset recorded in the LOG
    header is applied so the timestamps can be compared directly with the local
    DateTimeOriginal values stored in image EXIF metadata.
    """
    offset = timedelta(0)
    altitude_by_time = {}
    fixes = []

    lines = open(log_path, encoding="utf-8", errors="replace").read().splitlines()

    if lines and lines[0].startswith("@"):
        offset = parse_offset_from_header(lines[0])

    for line in lines:
        parts = line.split(",")

        if line.startswith("$GPGGA") and len(parts) > 9:
            gps_time = parts[1]
            altitude = parts[9]

            if gps_time and altitude:
                altitude_by_time[gps_time] = altitude

        elif line.startswith("$GPRMC") and len(parts) > 9:
            gps_time = parts[1]
            status = parts[2]
            lat = parts[3]
            lat_ref = parts[4]
            lon = parts[5]
            lon_ref = parts[6]
            gps_date = parts[9]

            if status != "A":
                continue

            if not gps_time or not gps_date or not lat or not lon:
                continue

            year, month, day = parse_nmea_date(gps_date)
            hour, minute, second = parse_nmea_time(gps_time)

            utc_dt = datetime(
                year,
                month,
                day,
                int(hour),
                int(minute),
                int(second),
            )

            local_dt = utc_dt + offset

            fixes.append({
                "datetime": local_dt,
                "latitude": str(nmea_to_decimal(lat, lat_ref)),
                "longitude": str(nmea_to_decimal(lon, lon_ref)),
                "altitude": altitude_by_time.get(gps_time, "NA"),
                "gps_utc_time": utc_dt.isoformat(sep=" "),
                "gps_local_time": local_dt.isoformat(sep=" "),
            })

    return fixes


def find_nearest_gps(image_dt, fixes):
    """
    Find the GPS fix closest in time to the image timestamp.

    Only fixes within MAX_TIME_DIFFERENCE_SECONDS are accepted. This prevents
    assigning uncertain coordinates to images when GPS logging contains long
    gaps (the camera clock and GPS logger are not sufficiently synchronized).
    """
    if image_dt is None:
        return None, None

    nearest = None
    nearest_diff = None

    for fix in fixes:
        diff = abs((image_dt - fix["datetime"]).total_seconds())

        if nearest is None or diff < nearest_diff:
            nearest = fix
            nearest_diff = diff

    if nearest is None or nearest_diff > MAX_TIME_DIFFERENCE_SECONDS:
        return None, nearest_diff

    return nearest, nearest_diff


def osm_url(lat, lon):
    """Create a direct OpenStreetMap link from latitude and longitude."""
    return (
        "https://www.openstreetmap.org/"
        f"?mlat={lat}&mlon={lon}#map=16/{lat}/{lon}"
    )


def has_geolocation_annotation(image, namespace):
    """
    Check whether the image already has a MapAnnotation in this namespace.

    This prevents duplicate geolocation annotations.
    """
    for ann in image.listAnnotations():
        if "MapAnnotation" not in type(ann).__name__:
            continue

        if ann.getNs() == namespace:
            return True

    return False


def add_annotation(conn, image, image_dt, gps, log_filename,
                   source_file, time_diff, namespace):
    """
    Create and link one geolocation MapAnnotation.

    Besides latitude, longitude and altitude, additional matching fields are
    stored. These allow users to audit which GPS fix was selected and how
    closely it matched the image timestamp.
    """
    lat = gps["latitude"]
    lon = gps["longitude"]

    values = [
        ["latitude", lat],
        ["longitude", lon],
        ["altitude", gps["altitude"]],
        ["osm_url", osm_url(lat, lon)],
        ["source", "GPS LOG"],
        ["source_file", source_file],
        ["gps_log_file", log_filename],
        ["image_datetime_original", image_dt.isoformat(sep=" ")],
        ["matched_gps_local_time", gps["gps_local_time"]],
        ["matched_gps_utc_time", gps["gps_utc_time"]],
        ["time_difference_seconds", str(round(time_diff, 2))],
    ]

    ann = MapAnnotationWrapper(conn)
    ann.setNs(namespace)
    ann.setValue(values)
    ann.save()

    image.linkAnnotation(ann)

    return ann


def get_images_to_process(conn, data_type, ids):
    """
    Accept either Dataset IDs or Image IDs from the OMERO.web form.

    Dataset mode processes all Images inside the selected Dataset.
    Image mode processes only the selected Image IDs.
    """
    images = []
    source_objects = []

    for object_id in ids:
        obj = conn.getObject(data_type, object_id)

        if obj is None:
            continue

        source_objects.append(obj)

        if data_type == "Image":
            images.append(obj)

        elif data_type == "Dataset":
            images.extend(list(obj.listChildren()))

    return images, source_objects


def get_namespace(client):
    """
    Resolve the namespace selected by the user.

    The script supports the default OMERO geolocation namespace, the
    GeoSPARQL namespace, or a completely custom namespace.
    """
    namespace = client.getInput(P_NAMESPACE, unwrap=True)
    custom_namespace = client.getInput(P_CUSTOM_NAMESPACE, unwrap=True)

    if namespace == "Custom":
        if not custom_namespace:
            raise ValueError(
                "Custom namespace selected but no custom namespace was provided."
            )
        return custom_namespace

    return namespace


def process_image(conn, image, fixes, log_filename, namespace):
    """
    Process one Image using the GPS logging file.

    The image timestamp is read from EXIF DateTimeOriginal and compared with all
    GPS fixes from the LOG file. If a sufficiently close match is found, a
    geolocation MapAnnotation is created.
    """
    if has_geolocation_annotation(image, namespace):
        return "already_annotated", None

    selected = None

    # Select the first supported original image file linked to this Image.
    for file_id, file_name in find_original_files(conn, image.getId()):
        suffix = os.path.splitext(file_name)[1].lower()

        if suffix in IMAGE_EXTENSIONS:
            selected = (file_id, file_name)
            break

    if selected is None:
        return "no_original_file", None

    file_id, file_name = selected
    temp_path = download_original_file(conn, file_id, file_name)

    try:
        image_dt = get_image_datetime(temp_path)

        if image_dt is None:
            return "no_datetime", None

        gps, time_diff = find_nearest_gps(image_dt, fixes)

        if gps is None:
            return "no_close_fix", time_diff

        ann = add_annotation(
            conn,
            image,
            image_dt,
            gps,
            log_filename,
            file_name,
            time_diff,
            namespace,
        )

        return "annotated", ann.getId()

    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def run_script():
    client = scripts.client(
        "Add GPS annotations with file",
        (
            "Match image timestamps against an attached GPS logging file "
            "and create geolocation MapAnnotations for the selected "
            "Images or Dataset."
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
            P_FILE_ANN,
            optional=True,
            grouping="1.2",
            description=(
                "GPS LOG FileAnnotation. If blank, the newest attached "
                ".LOG/.NMEA/.TXT file on the selected object is used."
            ),
        ),

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
        version="0.3.0",
    )

    try:
        data_type = client.getInput(P_DTYPE, unwrap=True)
        ids = client.getInput(P_IDS, unwrap=True)
        file_ann_input = client.getInput(P_FILE_ANN, unwrap=True)
        namespace = get_namespace(client)

        conn = BlitzGateway(client_obj=client)

        # ExifTool must be installed in the OMERO.server environment.
        if shutil.which("exiftool") is None:
            client.setOutput("ERROR", rstring("exiftool is not available."))
            return

        images, source_objects = get_images_to_process(conn, data_type, ids)

        if not images:
            client.setOutput("Message", rstring("No Images found to process."))
            return

        file_ann = get_log_file_annotation(conn, source_objects, file_ann_input)
        log_path, log_filename = download_file_annotation(conn, file_ann)

        try:
            fixes = load_gps_log(log_path)
        finally:
            if os.path.exists(log_path):
                os.remove(log_path)

        if not fixes:
            client.setOutput("Message", rstring("No valid GPS fixes found in LOG file."))
            return

        annotated = 0
        already_annotated = 0
        no_datetime = 0
        no_close_fix = 0
        no_original = 0
        result_obj = None

        # Process each image and count the outcome for the Activity summary.
        for image in images:
            status, detail = process_image(conn, image, fixes, log_filename, namespace)

            if status == "annotated":
                annotated += 1
                if result_obj is None:
                    result_obj = image

            elif status == "already_annotated":
                already_annotated += 1

            elif status == "no_datetime":
                no_datetime += 1

            elif status == "no_close_fix":
                no_close_fix += 1

            elif status == "no_original_file":
                no_original += 1

        message = (
            f"Input type: {data_type}\n"
            f"Input IDs: {ids}\n"
            f"Images processed: {len(images)}\n"
            f"GPS fixes in LOG: {len(fixes)}\n"
            f"GPS LOG file: {log_filename}\n"
            f"Max time difference: {MAX_TIME_DIFFERENCE_SECONDS} seconds\n\n"
            f"Annotated: {annotated}\n"
            f"Already annotated: {already_annotated}\n"
            f"Without DateTimeOriginal: {no_datetime}\n"
            f"Without close GPS fix: {no_close_fix}\n"
            f"Without supported original file: {no_original}\n\n"
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