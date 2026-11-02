SRC := ./report.md
DST := ./report.pdf
FRONT_DIR := front
FRONT := ${FRONT_DIR}/front.pdf
FULL := ./report_full.pdf

view: pdf
	sioyek report.pdf

pdf: report.md
	pandoc ${SRC} -o ${DST} --pdf-engine=typst --toc

# declaration pages (title, declaration, certificate) from front/front.tex
front: ${FRONT}

${FRONT}: ${FRONT_DIR}/front.tex ${FRONT_DIR}/Thesis.cls
	cd ${FRONT_DIR} && pdflatex -interaction=nonstopmode front.tex >/dev/null && pdflatex -interaction=nonstopmode front.tex >/dev/null

# front pages followed by the report
full: ${FRONT} pdf
	pdfunite ${FRONT} ${DST} ${FULL}

view-full: full
	sioyek ${FULL}

clean:
	rm -f report.pdf report_full.pdf
	cd ${FRONT_DIR} && rm -f front.pdf *.aux *.log *.out *.toc

.PHONY: view view-full pdf front full clean
