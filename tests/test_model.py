from app.model.document import Document


doc = Document(

    file_name="test.pdf",

    file_type="pdf"

)


print(
    doc.model_dump_json(
        indent=2
    )
)