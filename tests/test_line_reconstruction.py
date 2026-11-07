from conversion.lines import reconstruct_lines
from conversion.models import PageResult, QualityStatus, TextBlock, TextSource

def test_reconstruct_lines_orders_by_vertical_then_horizontal_position():
    result = PageResult(page_number=1,width=600,height=800,source=TextSource.TESSERACT,quality=QualityStatus.HIGH,blocks=[
        TextBlock("right",(0.6,0.1,0.9,0.15),"text",90,TextSource.TESSERACT,1,1),
        TextBlock("lower",(0.1,0.3,0.5,0.35),"text",80,TextSource.TESSERACT,2,1),
        TextBlock("left",(0.1,0.1,0.4,0.15),"text",95,TextSource.TESSERACT,1,1),
    ])
    lines=reconstruct_lines(result)
    assert [line.text for line in lines] == ["left","right","lower"]
    assert [line.line_index for line in lines] == [1,2,3]
    assert lines[0].to_dict()["bbox"] == [0.1,0.1,0.4,0.15]

def test_reconstruct_lines_ignores_empty_blocks_and_preserves_text():
    result=PageResult(page_number=2,width=100,height=100,source=TextSource.TESSERACT,quality=QualityStatus.REVIEW,blocks=[
        TextBlock("  ",(0,0,0,0),"text",None,TextSource.TESSERACT),
        TextBlock("  Hallo   wereld  ",(0.1,0.2,0.8,0.3),"text",75,TextSource.TESSERACT),
    ])
    lines=reconstruct_lines(result)
    assert len(lines)==1
    assert lines[0].text=="Hallo   wereld"
