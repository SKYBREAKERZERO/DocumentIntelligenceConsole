from app.pipeline.ingestion_pipeline import IngestionPipeline

from app.validator.document_validator import DocumentValidator



pipeline = IngestionPipeline()



document = pipeline.run(

    "./input/test.pdf",

    "./output/test.json"

)



print()

print("====================")

print(
    "Pipeline Result"
)

print("====================")



print(
    "Chapters:",
    len(document.chapters)
)


print(
    "Sections:",
    len(document.sections)
)


print(
    "Contents:",
    len(document.contents)
)



print()

print("====================")

print(
    "Validation"
)

print("====================")



validator = DocumentValidator()



result = validator.validate(

    document

)



print(
    "Valid:",
    result["valid"]
)



print()


print(
    "Errors:"
)


if result["errors"]:


    for error in result["errors"]:

        print(
            error
        )


else:

    print(
        "None"
    )



print()


print(
    "Warnings:"
)


if result["warnings"]:


    for warning in result["warnings"]:

        print(
            warning
        )


else:

    print(
        "None"
    )