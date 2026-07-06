#!/usr/bin/env python3

import csv
import os
import shutil
import subprocess
import tempfile

import omero.scripts as scripts
from omero.gateway import BlitzGateway
from omero.rtypes import rstring, rlong, robject
from omero.sys import ParametersI


P_DTYPE = "Data_Type"
P_IDS = "IDs"

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".tif", ".tiff", ".png"}

CSV_NS = "openmicroscopy.org/omero/geolocation/gps_csv"
DEFAULT_IMPORT_NS = "openmicroscopy.org/omero/client/mapAnnotation/geolocation"


def find_original_files(conn, image_id):
    """
    Find the original imported file associated with an OMERO Image.

    GPS metadata is stored in the original image file, so the script needs
    the OriginalFile rather than the rendered image shown in OMERO.web.
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
    Extract the core GPS values used by the geolocation annotation.

    Latitude and longitude are required. Altitude is optional and stored
    as NA if it is not present.
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


def get_images_to_process(conn, data_type, ids):
    """
    Accept either Dataset IDs or Image IDs from the OMERO.web form.

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


def create_csv_for_images(conn, images, label):
    """Create a temporary CSV containing GPS metadata for selected Images."""
    fd, csv_path = tempfile.mkstemp(
        prefix=f"gps_metadata_{label}_",
        suffix=".csv"
    )
    os.close(fd)

    rows_with_gps = 0
    rows_without_gps = 0
    rows_without_original = 0

    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)

        writer.writerow([
            "OBJECT_ID",
            "OBJECT_NAME",
            "latitude",
            "longitude",
            "altitude",
            "osm_url",
            "source",
            "source_file",
        ])

        for image in images:
            image_id = image.getId()
            image_name = image.getName()

            selected = None

            for file_id, file_name in find_original_files(conn, image_id):
                suffix = os.path.splitext(file_name)[1].lower()

                if suffix in IMAGE_EXTENSIONS:
                    selected = (file_id, file_name)
                    break

            if selected is None:
                rows_without_original += 1
                writer.writerow([image_id, image_name, "", "", "", "", "", ""])
                continue

            file_id, file_name = selected
            temp_path = download_original_file(conn, file_id, file_name)

            try:
                gps = extract_gps(temp_path)

                if gps is None:
                    rows_without_gps += 1
                    writer.writerow([
                        image_id,
                        image_name,
                        "",
                        "",
                        "",
                        "",
                        "",
                        file_name,
                    ])
                    continue

                lat = gps["latitude"]
                lon = gps["longitude"]

                writer.writerow([
                    image_id,
                    image_name,
                    lat,
                    lon,
                    gps["altitude"],
                    osm_url(lat, lon),
                    "EXIF GPS",
                    file_name,
                ])

                rows_with_gps += 1

            finally:
                if os.path.exists(temp_path):
                    os.remove(temp_path)

    return csv_path, rows_with_gps, rows_without_gps, rows_without_original


def run_script():
    client = scripts.client(
        "GPS extract",
        (
            "Extract GPS EXIF metadata from original files and create a CSV "
            "for review or import as geolocation MapAnnotations."
        ),

        scripts.String(
            P_DTYPE,
            optional=False,
            grouping="1",
            values=[rstring("Dataset"), rstring("Image")],
            default="Dataset",
            description="Export GPS metadata from a Dataset or selected Images.",
        ),

        scripts.List(
            P_IDS,
            optional=False,
            grouping="1.1",
            description="Dataset ID(s) or Image ID(s) to process.",
        ).ofType(rlong(0)),

        authors=["Daniel Olvera"],
        institutions=["MPI-EvolBio"],
        contact="https://forum.image.sc/tag/omero",
        version="0.8.0",
    )

    try:
        data_type = client.getInput(P_DTYPE, unwrap=True)
        ids = client.getInput(P_IDS, unwrap=True)

        conn = BlitzGateway(client_obj=client)

        if shutil.which("exiftool") is None:
            client.setOutput("ERROR", rstring("exiftool is not available."))
            return

        images, source_objects = get_images_to_process(conn, data_type, ids)

        if not images:
            client.setOutput("Message", rstring("No Images found to process."))
            return

        label = f"{data_type}_{'_'.join(str(i) for i in ids)}"

        csv_path, with_gps, without_gps, without_original = create_csv_for_images(
            conn,
            images,
            label,
        )

        target = source_objects[0] if source_objects else images[0]

        file_ann = conn.createFileAnnfromLocalFile(
            csv_path,
            mimetype="text/csv",
            ns=CSV_NS,
            desc=(
                "GPS metadata CSV generated from original image EXIF. "
                f"Suggested Import from CSV namespace: {DEFAULT_IMPORT_NS}"
            ),
        )

        target.linkAnnotation(file_ann)

        if os.path.exists(csv_path):
            os.remove(csv_path)

        message = (
            f"Input type: {data_type}\n"
            f"Input IDs: {ids}\n"
            f"Images processed: {len(images)}\n\n"
            f"Rows with GPS: {with_gps}\n"
            f"Rows without GPS: {without_gps}\n"
            f"Rows without supported original file: {without_original}\n\n"
            f"CSV FileAnnotation: {file_ann.getId()}\n\n"
            f"Suggested Import from CSV namespace:\n{DEFAULT_IMPORT_NS}"
        )

        client.setOutput("Message", rstring(message))
        client.setOutput("Result", robject(file_ann._obj))

    except Exception as err:
        client.setOutput("ERROR", rstring(str(err)))
        raise

    finally:
        client.closeSession()


if __name__ == "__main__":
    run_script()