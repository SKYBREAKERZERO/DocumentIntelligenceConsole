from app.analyzer.title_detector import TitleDetector



tests = [

    "1 Bluetooth Audio Function",

    "1.1 Purpose",

    "1.1.1 Connection Type",

    "Revision History",

    "This specification defines function"

]


for line in tests:


    result = TitleDetector.detect(
        line
    )


    print(
        line
    )

    print(
        result
    )

    print(
        "-----"
    )