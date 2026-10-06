class DocumentLoaderException(Exception):
    """
    Base exception for document loading.
    """
    pass

class UnsupportedFileTypeException(
    DocumentLoaderException
):
    """
    Unsupported document format.
    """
    pass

class FileNotFoundException(
    DocumentLoaderException
):
    """
    Input file does not exist.
    """
    pass

class FileReadException(
    DocumentLoaderException
):
    """
    File cannot be read.
    """
    pass