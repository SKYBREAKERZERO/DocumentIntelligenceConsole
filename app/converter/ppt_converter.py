from __future__ import annotations

from pathlib import Path


class PPTConversionError(RuntimeError):
    """Legacy .ppt -> .pptx conversion error."""


class PPTConverter:
    """
    Convert legacy binary PowerPoint (.ppt) to .pptx on Windows.

    Requirement:
        - Microsoft PowerPoint installed
        - pywin32 installed

    PowerPoint COM SaveAs format:
        24 = ppSaveAsOpenXMLPresentation
    """

    PP_SAVE_AS_OPEN_XML_PRESENTATION = 24

    def convert(
        self,
        input_path: str | Path,
        output_path: str | Path,
    ) -> Path:

        source = self._validate_source(
            input_path
        )

        target = Path(
            output_path
        ).expanduser().resolve()

        if target.suffix.lower() != ".pptx":
            raise ValueError(
                "PPT conversion output must end with .pptx."
            )

        target.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        try:
            import pythoncom
            import win32com.client

        except ImportError as exc:
            raise PPTConversionError(
                "Legacy PPT conversion requires pywin32. "
                "Install it with: "
                "python -m pip install pywin32"
            ) from exc

        application = None
        presentation = None

        pythoncom.CoInitialize()

        try:
            try:
                application = (
                    win32com.client.DispatchEx(
                        "PowerPoint.Application"
                    )
                )

            except Exception as exc:
                raise PPTConversionError(
                    "Microsoft PowerPoint could not be started. "
                    "Legacy .ppt support requires Microsoft "
                    "PowerPoint to be installed on this computer."
                ) from exc

            try:
                application.Visible = 0
            except Exception:
                # Some Office builds reject setting Visible=0;
                # WithWindow=False below is still sufficient.
                pass

            try:
                presentation = (
                    application.Presentations.Open(
                        str(
                            source
                        ),
                        ReadOnly=True,
                        Untitled=False,
                        WithWindow=False,
                    )
                )

            except Exception as exc:
                raise PPTConversionError(
                    f"PowerPoint could not open "
                    f"'{source.name}': {exc}"
                ) from exc

            try:
                presentation.SaveAs(
                    str(
                        target
                    ),
                    self.PP_SAVE_AS_OPEN_XML_PRESENTATION,
                )

            except Exception as exc:
                raise PPTConversionError(
                    f"PowerPoint could not convert "
                    f"'{source.name}' to PPTX: {exc}"
                ) from exc

        finally:

            if presentation is not None:

                try:
                    presentation.Close()

                except Exception:
                    pass

            if application is not None:

                try:
                    application.Quit()

                except Exception:
                    pass

            pythoncom.CoUninitialize()

        if not target.is_file():
            raise PPTConversionError(
                "PowerPoint conversion completed without "
                "creating the expected PPTX file."
            )

        return target

    @staticmethod
    def _validate_source(
        input_path: str | Path,
    ) -> Path:

        path = Path(
            input_path
        ).expanduser().resolve()

        if not path.exists():
            raise FileNotFoundError(
                f"PPT file not found: {path}"
            )

        if not path.is_file():
            raise IsADirectoryError(
                f"Input path is not a file: {path}"
            )

        if path.name.startswith(
            "~$"
        ):
            raise ValueError(
                "Temporary PowerPoint file is not supported: "
                f"{path.name}"
            )

        if path.suffix.lower() != ".ppt":
            raise ValueError(
                "PPTConverter only accepts .ppt files. "
                f"Received: "
                f"{path.suffix or '<no extension>'}"
            )

        return path
