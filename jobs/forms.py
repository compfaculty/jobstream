from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User
from django.utils.html import conditional_escape

from .models import Application, Document


class RegistrationForm(UserCreationForm):
    class Meta:
        model = User
        fields = ("username",)


class ApplicationForm(forms.ModelForm):
    class Meta:
        model = Application
        fields = ("recipient", "subject", "body", "document")
        widgets = {"body": forms.Textarea(attrs={"rows": 10})}

    def __init__(self, *args, owner, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["recipient"].required = self.instance.channel == "email"
        self.fields["document"].queryset = Document.objects.filter(owner=owner)
        self.fields["document"].label_from_instance = lambda obj: obj.name
        from .services.secret_telaviv import supported

        self.secret_portal = (
            self.instance.channel == "portal" and supported(self.instance)
            if self.instance.job_id
            else False
        )
        if self.secret_portal:
            self.fields["document"].required = True
            self.fields["body"].required = False
            self.fields["body"].label = "Private note (not sent to Secret Tel Aviv)"
            for name, field in SecretApplicantForm.base_fields.items():
                import copy

                self.fields[f"secret_{name}"] = copy.deepcopy(field)
                self.initial[f"secret_{name}"] = self.instance.portal_answers.get(
                    name, False if name in {"agree_terms", "forward_headhunters"} else ""
                )
        from .services.cato_greenhouse import supported as cato_supported

        self.cato_portal = bool(self.instance.job_id and cato_supported(self.instance))
        if self.cato_portal:
            import copy

            from .services.cato_greenhouse import FIELDS
            from .services.saved_answers import read_answers

            defaults = {}
            if not self.instance.pk and not self.is_bound:
                try:
                    saved, reuse = read_answers(owner.pk)
                    defaults = {
                        key: value for key, value in saved.items() if reuse and key in FIELDS
                    }
                except forms.ValidationError:
                    pass  # Saving a draft is still possible with manually entered answers.
            self.fields["document"].required = True
            self.fields["body"].required = False
            self.fields["body"].label = "Private note (not sent to Cato)"
            for name in CatoApplicantForm.base_fields:
                self.fields[f"cato_{name}"] = copy.deepcopy(CatoApplicantForm.base_fields[name])
                self.fields[f"cato_{name}"].required = name != "linkedin"
                self.initial[f"cato_{name}"] = self.instance.portal_answers.get(
                    name, defaults.get(name, "")
                )
        from .services import camunda_ashby

        self.camunda_portal = bool(self.instance.job_id and camunda_ashby.supported(self.instance))
        self.camunda_schema = None
        if self.camunda_portal:
            self.fields["document"].required = True
            self.fields["body"].required = False
            self.fields["body"].label = "Private note (not sent to Camunda)"
            try:
                self.camunda_schema = camunda_ashby.read_schema(self.instance.job)
            except camunda_ashby.PortalRejected, camunda_ashby.PortalRateLimited:
                self.fields["camunda_unavailable"] = forms.CharField(
                    required=False,
                    disabled=True,
                    initial="Camunda form is unavailable; try again later.",
                )
            else:
                reuse_answers = self.instance.portal_fingerprint == self.camunda_schema.fingerprint
                if self.instance.pk and not reuse_answers:
                    self.fields["schema_notice"] = forms.CharField(
                        required=False,
                        disabled=True,
                        initial="Camunda's questions or notices changed. Re-enter and review "
                        "answers; previous consent choices were cleared.",
                    )
                self.fields["camunda_schema"] = forms.CharField(widget=forms.HiddenInput)
                self.initial["camunda_schema"] = self.camunda_schema.fingerprint
                for entry in self.camunda_schema.entries:
                    field = entry["field"]
                    path, kind = field["path"], field["type"]
                    if kind == "File":
                        continue
                    options = [(v["value"], v["label"]) for v in field.get("selectableValues", [])]
                    if kind == "Boolean":
                        form_field = forms.ChoiceField(
                            choices=[("", "Choose…"), ("yes", "Yes"), ("no", "No")]
                        )
                    elif kind == "MultiValueSelect":
                        form_field = forms.MultipleChoiceField(
                            choices=options, widget=forms.CheckboxSelectMultiple
                        )
                    elif kind == "ValueSelect":
                        form_field = forms.ChoiceField(choices=[("", "Leave unanswered"), *options])
                    elif kind == "Email":
                        form_field = forms.EmailField()
                    else:
                        form_field = forms.CharField(max_length=2000)
                    form_field.label = field["title"] or "Application data consent"
                    form_field.required = entry["isRequired"]
                    form_field.help_text = camunda_ashby.plain(entry.get("descriptionHtml"))
                    if kind == "Location":
                        form_field.help_text += (
                            " Enter the exact city/country suggestion shown on the employer form."
                        )
                    form_field.help_text = conditional_escape(form_field.help_text)
                    self.fields[f"camunda_{path}"] = form_field
                    default = [] if kind == "MultiValueSelect" else ""
                    self.initial[f"camunda_{path}"] = (
                        self.instance.portal_answers.get(path, default)
                        if reuse_answers
                        else default
                    )
                if self.camunda_schema.payload.get("shouldAskForTextingConsent"):
                    self.fields["camunda_sms_consent"] = forms.ChoiceField(
                        required=False,
                        label="Receive application updates by SMS",
                        choices=[
                            ("", "Leave unanswered"),
                            ("yes", "Yes — I consent"),
                            ("no", "No — I do not consent"),
                        ],
                        help_text="Camunda Inc. may send recruiting texts; frequency varies "
                        "and message/data rates may apply. Reply STOP to opt out. "
                        + self.camunda_schema.payload.get("candidateTextingPrivacyPolicyUrl", "")
                        + " "
                        + self.camunda_schema.payload.get(
                            "candidateTextingTermsAndConditionsUrl", ""
                        ),
                    )
                    self.initial["camunda_sms_consent"] = (
                        self.instance.portal_answers.get("sms_consent", "") if reuse_answers else ""
                    )
                    self.fields["camunda_sms_consent"].help_text = conditional_escape(
                        self.fields["camunda_sms_consent"].help_text
                    )

        from .services import gitlab_greenhouse as gitlab

        self.gitlab_portal = bool(self.instance.job_id and gitlab.supported(self.instance))
        self.gitlab_schema = None
        if self.gitlab_portal:
            self.fields["document"].required = True
            self.fields["body"].required = False
            self.fields["body"].label = "Private note (not sent to GitLab)"
            try:
                self.gitlab_schema = gitlab.read_schema(self.instance.job)
            except gitlab.PortalRejected, gitlab.PortalRateLimited:
                self.fields["gitlab_unavailable"] = forms.CharField(
                    required=False, disabled=True, initial="GitLab form unavailable; try later."
                )
            else:
                schema = self.gitlab_schema
                reuse = self.instance.portal_fingerprint == schema.fingerprint
                if self.instance.pk and not reuse:
                    self.fields["schema_notice"] = forms.CharField(
                        required=False,
                        disabled=True,
                        initial="GitLab questions changed. Re-enter and review answers.",
                    )
                self.fields["gitlab_schema"] = forms.CharField(widget=forms.HiddenInput)
                self.initial["gitlab_schema"] = schema.fingerprint
                for q in schema.entries:
                    f = q["fields"][0]
                    if f["type"] == "multi_value_single_select":
                        field = forms.ChoiceField(
                            choices=[
                                ("", "Leave unanswered"),
                                *((v["value"], v["label"]) for v in f["values"]),
                            ]
                        )
                    elif f["name"] == "email":
                        field = forms.EmailField()
                    else:
                        field = forms.CharField(
                            max_length=4000,
                            widget=forms.Textarea if f["type"] == "textarea" else forms.TextInput,
                        )
                    field.label = q["label"]
                    field.required = q["required"]
                    from .services.normalization import text_blocks

                    field.help_text = conditional_escape(text_blocks(q.get("description") or ""))
                    key = "gitlab_" + f["name"]
                    self.fields[key] = field
                    self.initial[key] = (
                        self.instance.portal_answers.get(f["name"], "") if reuse else ""
                    )
                self.fields["gitlab_phone_country"] = forms.CharField(
                    max_length=2, required=False, label="Phone country (ISO code, e.g. IL)"
                )
                self.initial["gitlab_phone_country"] = (
                    self.instance.portal_answers.get("phone_country", "") if reuse else ""
                )

    def clean(self):
        values = super().clean()
        if any(c in values.get("recipient", "") + values.get("subject", "") for c in "\r\n"):
            raise forms.ValidationError("Headers cannot contain newlines.")
        if self.secret_portal:
            values["body"] = values.get("body") or "Secret Tel Aviv application."
            from .services.secret_telaviv import TERMS_URL, validate_answers

            answers = {
                name: values.get(f"secret_{name}") for name in SecretApplicantForm.base_fields
            }
            answers["terms_url"] = TERMS_URL
            if not self.errors:
                validate_answers(answers)
                self.instance.portal_answers = values["portal_answers"] = answers
        if self.cato_portal:
            from .services.cato_greenhouse import validate_answers

            values["body"] = values.get("body") or "Cato Networks application."
            answers = {
                name: values.get(f"cato_{name}", "") for name in CatoApplicantForm.base_fields
            }
            if not self.errors:
                validate_answers(answers)
                self.instance.portal_answers = values["portal_answers"] = answers
        if self.camunda_portal:
            from .services.camunda_ashby import validate_schema_answers

            if self.camunda_schema is None:
                raise forms.ValidationError(
                    "Camunda form unavailable; no application can be approved."
                )
            if values.get("camunda_schema") != self.camunda_schema.fingerprint:
                raise forms.ValidationError(
                    "Camunda questions changed; reload and review them again."
                )
            values["body"] = values.get("body") or "Camunda application."
            answers = {
                name.removeprefix("camunda_"): value
                for name, value in values.items()
                if name.startswith("camunda_") and name != "camunda_schema"
            }
            answers["_schema"] = self.camunda_schema.payload
            if not self.errors:
                validate_schema_answers(self.camunda_schema, answers)
                self.instance.portal_answers = values["portal_answers"] = answers
                self.instance.portal_fingerprint = values["portal_fingerprint"] = (
                    self.camunda_schema.fingerprint
                )
        if self.gitlab_portal:
            from .services.gitlab_greenhouse import validate_answers

            if self.gitlab_schema is None:
                raise forms.ValidationError("GitLab form unavailable; approval is disabled.")
            if values.get("gitlab_schema") != self.gitlab_schema.fingerprint:
                raise forms.ValidationError("GitLab questions changed; reload and review again.")
            values["body"] = values.get("body") or "GitLab application."
            answers = {
                k.removeprefix("gitlab_"): v
                for k, v in values.items()
                if k.startswith("gitlab_") and k != "gitlab_schema"
            }
            answers["_schema"] = self.gitlab_schema.payload
            if not self.errors:
                validate_answers(answers)
                self.instance.portal_answers = values["portal_answers"] = answers
                self.instance.portal_fingerprint = values["portal_fingerprint"] = (
                    self.gitlab_schema.fingerprint
                )
        return values


class TrackingForm(forms.ModelForm):
    class Meta:
        model = Application
        fields = ("tracking_status", "notes")


class UploadForm(forms.Form):
    file = forms.FileField(label="Resume (PDF, maximum 5 MiB)")


class ResumeProfileForm(forms.Form):
    name = forms.CharField(
        max_length=150, label="Full name", widget=forms.TextInput(attrs={"autocomplete": "name"})
    )
    headline = forms.CharField(
        max_length=200,
        required=False,
        label="Professional headline",
        help_text=(
            "Your role and specialties. Example: Senior Software Engineer | Cloud & Networking"
        ),
    )
    email = forms.EmailField(
        required=False,
        label="Email address",
        widget=forms.EmailInput(attrs={"autocomplete": "email"}),
    )
    phone = forms.CharField(
        max_length=50,
        required=False,
        label="Phone number",
        help_text="Include your country code, for example +972 50 123 4567.",
        widget=forms.TextInput(attrs={"type": "tel", "autocomplete": "tel"}),
    )
    location = forms.CharField(
        max_length=150,
        required=False,
        label="City, region and country",
        help_text="Example: Tel Aviv District, Israel. No street address needed.",
    )
    linkedin = forms.URLField(
        max_length=300, required=False, label="LinkedIn URL", assume_scheme="https"
    )
    website = forms.URLField(
        max_length=300, required=False, label="Portfolio or personal website", assume_scheme="https"
    )
    github = forms.URLField(
        max_length=300, required=False, label="GitHub or code portfolio URL", assume_scheme="https"
    )
    contact = forms.CharField(
        max_length=500,
        required=False,
        label="Other / previously saved contact details",
        help_text="Move older contact details into the fields above when convenient.",
    )
    summary = forms.CharField(
        required=False,
        label="About you",
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text=(
            "In 3–5 sentences: what you do, your areas of expertise, and the problems you solve."
        ),
    )
    skills = forms.CharField(
        required=False,
        label="Skills and technologies",
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text=(
            "List specific tools and skills, separated by commas. Example: "
            "Python, Go, Linux, Kubernetes."
        ),
    )
    languages = forms.CharField(
        required=False,
        label="Languages and proficiency",
        help_text="Example: English — professional working; Ukrainian — native.",
    )
    certifications = forms.CharField(
        required=False,
        label="Certifications and licenses",
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text=(
            "One per line: credential, issuing organization, year, and credential URL if available."
        ),
    )
    experience = forms.CharField(
        required=False,
        label="Previously saved experience / additional context",
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    projects = forms.CharField(
        required=False,
        label="Previously saved projects / additional context",
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    education = forms.CharField(
        required=False,
        label="Previously saved education / additional context",
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    other = forms.CharField(
        required=False,
        label="Additional facts",
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text=(
            "Awards, publications, volunteering, or other relevant facts. "
            "Separate entries with a blank line."
        ),
    )
    confirmed = forms.BooleanField(
        label="I confirm these facts, including my additions, are accurate."
    )


class DatedProfileEntryForm(forms.Form):
    start = forms.DateField(
        required=False,
        label="Start date",
        input_formats=["%Y-%m"],
        widget=forms.DateInput(format="%Y-%m", attrs={"type": "month"}),
    )
    end = forms.DateField(
        required=False,
        label="End date",
        input_formats=["%Y-%m"],
        widget=forms.DateInput(format="%Y-%m", attrs={"type": "month"}),
    )
    current = forms.BooleanField(required=False, label="I currently work / study here")

    def clean(self):
        values = super().clean()
        if values.get("current") and values.get("end"):
            self.add_error("end", "Leave the end date empty for a current entry.")
        if values.get("start") and values.get("end") and values["end"] < values["start"]:
            self.add_error("end", "End date must be on or after the start date.")
        return values


class ProfileExperienceForm(DatedProfileEntryForm):
    field_order = [
        "title",
        "company",
        "employment_type",
        "location",
        "start",
        "end",
        "current",
        "description",
        "technologies",
    ]

    title = forms.CharField(
        max_length=200, label="Job title", help_text="Example: Senior Software Engineer"
    )
    company = forms.CharField(max_length=200, label="Company or organization")
    employment_type = forms.ChoiceField(
        required=False,
        label="Employment type",
        choices=[
            ("", "Select if relevant"),
            ("Full-time", "Full-time"),
            ("Part-time", "Part-time"),
            ("Contract", "Contract"),
            ("Freelance", "Freelance"),
            ("Internship", "Internship"),
            ("Self-employed", "Self-employed"),
        ],
    )
    location = forms.CharField(max_length=150, required=False, label="Location / remote")
    description = forms.CharField(
        max_length=3500,
        required=False,
        label="Responsibilities and achievements",
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text=(
            "One point per line: what you built or improved, how, and the "
            "result. Include metrics only if known."
        ),
    )
    technologies = forms.CharField(
        max_length=500, required=False, label="Technologies used in this role"
    )


class ProfileEducationForm(DatedProfileEntryForm):
    field_order = ["school", "degree", "field", "start", "end", "current", "description"]

    school = forms.CharField(max_length=250, label="School or university")
    degree = forms.CharField(max_length=200, required=False, label="Degree or qualification")
    field = forms.CharField(max_length=200, required=False, label="Field of study")
    description = forms.CharField(
        max_length=3000,
        required=False,
        label="Relevant coursework, thesis or honors",
        widget=forms.Textarea(attrs={"rows": 3}),
    )


class ProfileProjectForm(forms.Form):
    name = forms.CharField(max_length=200, label="Project name")
    role = forms.CharField(max_length=200, required=False, label="Your role / contribution")
    url = forms.URLField(
        max_length=300, required=False, label="Project or repository URL", assume_scheme="https"
    )
    technologies = forms.CharField(max_length=500, required=False, label="Technologies used")
    description = forms.CharField(
        max_length=3500,
        required=False,
        label="What you built and achieved",
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text=(
            "Describe the problem, your own contribution, and the outcome. "
            "Personal and AI projects are welcome."
        ),
    )


class SecretApplicantForm(forms.Form):
    first_name = forms.CharField(max_length=150, label="First name")
    last_name = forms.CharField(max_length=150, label="Family name")
    email = forms.EmailField(label="Applicant email")
    phone = forms.CharField(max_length=100, label="Telephone")
    agree_terms = forms.BooleanField(label="I accept the Secret Tel Aviv terms and conditions")
    forward_headhunters = forms.BooleanField(required=False, label="Forward my CV to headhunters")

    def answers(self):
        from .services.secret_telaviv import TERMS_URL

        return {**self.cleaned_data, "terms_url": TERMS_URL}


class ApplicationPreferencesForm(forms.Form):
    preferred_locations = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        label="Preferred locations",
        help_text="One country, city or region per line.",
    )
    location_required = forms.BooleanField(required=False, label="Only apply in these locations")
    preferred_titles = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        label="Preferred job titles",
        help_text="One title or title phrase per line.",
    )
    title_required = forms.BooleanField(required=False, label="Only apply to these titles")
    wanted_technologies = forms.MultipleChoiceField(
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label="Technologies I want to work with",
    )
    technology_required = forms.BooleanField(
        required=False, label="Require at least one wanted technology in the job"
    )
    excluded_technologies = forms.MultipleChoiceField(
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label="Do not apply when these technologies are mandatory",
    )
    remote_only = forms.BooleanField(required=False, label="Only apply to explicitly remote jobs")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from .services.normalization import TECHNOLOGIES

        choices = [(name, name) for name in TECHNOLOGIES]
        for field in ("excluded_technologies", "wanted_technologies"):
            self.fields[field].choices = choices

    def clean(self):
        data = super().clean()
        for field in ("preferred_locations", "preferred_titles"):
            values = list(
                dict.fromkeys(
                    line.strip() for line in data.get(field, "").splitlines() if line.strip()
                )
            )
            if len(values) > 30 or any(len(value) > 150 for value in values):
                self.add_error(field, "Use at most 30 lines, up to 150 characters per line.")
            data[field] = values
        for field in ("excluded_technologies", "wanted_technologies"):
            data[field] = list(dict.fromkeys(data.get(field, [])))
        if set(data["wanted_technologies"]) & set(data["excluded_technologies"]):
            self.add_error(
                "wanted_technologies", "A technology cannot be both wanted and excluded."
            )
        for flag, field in (
            ("location_required", "preferred_locations"),
            ("title_required", "preferred_titles"),
            ("technology_required", "wanted_technologies"),
        ):
            if data.get(flag) and not data.get(field):
                self.add_error(field, "Add at least one value before enabling this limit.")
        return data


class CommonAnswersForm(forms.Form):
    first_name = forms.CharField(required=False, max_length=150, label="First name")
    last_name = forms.CharField(required=False, max_length=150, label="Family name")
    email = forms.EmailField(required=False, label="Application email")
    phone = forms.RegexField(
        r"^\+[1-9]\d{6,14}$",
        required=False,
        max_length=16,
        label="Phone with country code",
        help_text="International format, for example +972501234567.",
    )
    phone_country = forms.RegexField(
        r"^[A-Za-z]{2}$",
        required=False,
        max_length=2,
        label="Phone country code",
        help_text="Two-letter country code, for example IL, US or GB.",
    )
    city = forms.CharField(required=False, max_length=150, label="Current city")
    country = forms.CharField(required=False, max_length=150, label="Current country")
    linkedin = forms.URLField(
        required=False, max_length=500, label="LinkedIn profile", assume_scheme="https"
    )
    notice_period = forms.CharField(required=False, max_length=150, label="Notice period")
    work_authorization_notes = forms.CharField(
        required=False,
        max_length=2000,
        widget=forms.Textarea(attrs={"rows": 3}),
        label="Work authorization notes",
        help_text="Country-specific facts for review; never automatically "
        "converted into a yes/no answer.",
    )
    gender_identity = forms.ChoiceField(
        required=False,
        label="Gender identity (optional)",
        choices=[
            ("", "Leave unanswered / ask when needed"),
            ("male", "Male"),
            ("female", "Female"),
            ("nonbinary", "Non-binary"),
            ("prefer_not_to_answer", "Prefer not to answer"),
        ],
        help_text="Only for an equivalent voluntary gender-identity question, "
        "not legal-sex questions.",
    )
    reuse_enabled = forms.BooleanField(
        required=False,
        label="Allow these saved answers to prefill compatible application questions",
    )

    def clean_phone_country(self):
        import phonenumbers

        value = self.cleaned_data["phone_country"].upper()
        if value and value not in phonenumbers.SUPPORTED_REGIONS:
            raise forms.ValidationError("Enter a supported two-letter country code.")
        return value


class CatoApplicantForm(CommonAnswersForm):
    """Only contact questions with established meaning; no demographic inference."""

    notice_period = None
    work_authorization_notes = None
    gender_identity = None
    reuse_enabled = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            field.required = name != "linkedin"
