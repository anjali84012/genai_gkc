import utils

db, app = utils.flask_sql_alchemy_db()

class Email(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    company_name = db.Column(db.String(255))
    subject = db.Column(db.String(255))
    date = db.Column(db.String(50))  # Date stored as 'DD-MM-YY'
    time = db.Column(db.String(50))
    link = db.Column(db.String(5000))
    title = db.Column(db.Text)
    body = db.Column(db.Text)
    # body = db.Column(db.Text)
    response_generated = db.Column(db.Boolean, default=False)
    category = db.Column(db.String(255))
    category_code = db.Column(db.String(50))
    summary = db.Column(db.Text)
    summary_japanese = db.Column(db.Text)
    decision = db.Column(db.String(50))  # New column for decision
    reasoning = db.Column(db.Text)  # New column for reasoning
    satake_score = db.Column(db.Integer, default=0)      # New column for Satake
    kajikawa_score = db.Column(db.Integer, default=0)    # New column for Kajikawa
    satake_done_scoring = db.Column(db.Boolean, default=False)
    kajikawa_done_scoring = db.Column(db.Boolean, default=False)
    total_score = db.Column(db.Integer, default=0)
    final_flag = db.Column(db.Boolean, default=False)
    remove_flag = db.Column(db.Boolean, default=False)
    disapproved = db.Column(db.Boolean, default=False)
    duplicated_or_not = db.Column(db.Boolean, default=False)
    reason_of_duplication = db.Column(db.Text)

    def __init__(self, company_name, subject, 
                 date, time, link, 
                 title, body, response_generated=False, category=None, 
                 category_code=None, summary=None, summary_japanese=None, 
                 decision=None, reasoning=None,
                 satake_score=0, kajikawa_score=0, satake_done_scoring=False, 
                 kajikawa_done_scoring=False, total_score=0,final_flag=False, 
                 remove_flag = False, disapproved = False, duplicated_or_not=False, reason_of_duplication=None):
        self.company_name = company_name
        self.subject = subject
        self.date = date
        self.time = time
        self.link = link
        self.title = title
        self.body = body
        self.response_generated = response_generated
        self.category = category
        self.category_code = category_code
        self.summary = summary
        self.summary_japanese = summary_japanese
        self.decision = decision
        self.reasoning = reasoning
        self.satake_score = satake_score
        self.kajikawa_score = kajikawa_score
        self.satake_done_scoring = satake_done_scoring
        self.kajikawa_done_scoring = kajikawa_done_scoring
        self.total_score = total_score
        self.final_flag = final_flag
        self.remove_flag = remove_flag
        self.disapproved = disapproved
        self.duplicated_or_not = duplicated_or_not
        self.reason_of_duplication = reason_of_duplication

urls_to_be_scrapped = [
    "https://www.nri.com/jp/News",
    "https://www.mckinsey.com/about-us/new-at-mckinsey-blog",  ## fetched from jina ai
    "https://www.mckinsey.com/jp/our-insights",
    "https://www.bcg.com/about/corporate-newsroom",
    "https://www.bcg.com/ja-jp/about/news/press/default?00000173-7007-d959-afff-79c791c80008-page=1",
    "https://www.bain.com/about/media-center/press-releases/",
    "https://www.bain.com/ja/about-bain/media-center/press-releases/",
    "https://newsroom.accenture.com/",
    "https://newsroom.accenture.jp/",
    "https://www.deloitte.com/global/en/about/press-room.html",
    "https://www2.deloitte.com/jp/ja/footerlinks/nr.html",
    "https://www.ey.com/en_gl/newsroom",
    "https://www.ey.com/ja_jp/newsroom",
    "https://www.pwc.com/gx/en/news-room.html",
    "https://www.pwc.com/jp/ja/press-room.html#press",
    "https://kpmg.com/xx/en/home/media/press-releases.html",
    "https://kpmg.com/jp/ja/home/media/press-releases.html",
    "https://www.mri.co.jp/news/index.html",
    "https://www.consulting.us/",
    "https://www.consultancy.uk/",
    "https://www.consultancy.asia/country/japan",
    "https://www.consultancy.in/",
    "https://www.consultancy.com.au/",
    "https://www.consultancy.eu/",
    "https://www.consultancy.asia/",
    "https://www.consultancy-me.com/",
    "https://www.consultancy.nl/",
    "https://www.consultancy.co.za/",
    "https://www.consultancy.lat/",
    "https://www.consultancy.africa/",
    "https://www.consulting.ca/",
    "https://www.consultancy.asia/country/singapore",
    "https://www.consultancy.asia/country/china",
    "https://www.consultancy.eu/country/france",
    "https://www.consultancy.eu/country/germany",
    "https://www.consultancy.uk/"                       ## Fetched from newspaper
]