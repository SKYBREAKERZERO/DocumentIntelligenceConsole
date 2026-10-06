from app.loader.loader_factory import LoaderFactory

file="./input/test.pdf"

loader = LoaderFactory.get_loader(
    file
)

document = loader.load(
    file
)

print(
    document.model_dump_json(
        indent=2
    )
)