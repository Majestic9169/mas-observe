SRC := ./report.md
DST := ./report.pdf

pdf: report.md
	pandoc ${SRC} -o ${DST} --pdf-engine=typst --toc

view: pdf
	sioyek report.pdf

clean: 
	rm report.pdf

.PHONY: pdf view clean
