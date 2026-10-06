from app.model.document import Document
from app.model.page import Page

from app.analyzer.structure_analyzer import StructureAnalyzer



doc = Document(

    file_name="test.pdf",

    file_type="pdf",

    pages=[

        Page(

            page_number=1,

            text="""
1 Bluetooth Audio Function

Introduction text

1.1 Purpose

Purpose text here

1.1.1 Connection Type

Bluetooth connection detail
"""
        )

    ]

)



analyzer = StructureAnalyzer()


result = analyzer.analyze(
    doc
)


print(
    result.model_dump_json(
        indent=2
    )
)