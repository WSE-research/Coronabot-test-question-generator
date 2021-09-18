import json
import logging
import sys
from requests.models import Response
import stardog # pip3 install pystardog
import pprint
import requests
import pandas as pd
import datetime
import os
import argparse
import importlib
import inspect
import curlify # pip3 install curlify

sheet_name = "QanarySystemQualityControl"
outdir = "output"


def connect_to_triplestore(conf_qanary, endpoint):
    """
    connect to the Qanary triplestore (Stardog connection)

    Args:
        conf_qanary ([type]): [description]
        endpoint ([type]): [description]

    Returns:
        [type]: [description]
    """
    conn_details = {
        'endpoint': conf_qanary.get("qanary_triplestore_endpoint"),
        'username': conf_qanary.get("qanary_triplestore_username"),
        'password': conf_qanary.get("qanary_triplestore_password")
    }
    database = conf_qanary.get("qanary_triplestore_database")

    with stardog.Admin(**conn_details) as admin:
        with stardog.Connection(database, **conn_details) as conn:
            return conn

    print("ERROR")
    return None

def prepare_sparql_query(logger, sparql_template_filename, replacements, graphid):
    #pprint.pprint(replacements)
    with open(sparql_template_filename, 'r') as file:
        data = file.read()

        if not "<GRAPHID>" in data:
            raise RuntimeError("The file '%s' does NOT contain the placeholder '<GRAPHID>'." % (sparql_template_filename,))

        if not "ASK" in data.upper():
            raise RuntimeError("The file '%s' seems NOT to be a ASK query (the string 'ASK' is not contained in the file)." % (sparql_template_filename,))

        data = data.replace("<GRAPHID>", f"<{graphid}>")

        for k in replacements.keys():
            #pprint.pprint(replacement.keys())
            v = replacements[k]
            logger.debug(f"replace: '{k}' by '{v}'")
            data = data.replace(k,str(v))

        return data
    
    return None

def request_qanary_endpoint_for_question(logger, conf_qanary, question):
    """
    request to Qanary endpoint that is defined within the configuration with a given question

    Args:
        conf_qanary ([type]): [description]
        question ([type]): [description]

    Returns:
        [type]: [description]
    """
    data = {
        "question": question,
        "componentlist[]": conf_qanary.get("componentlist")
    }
    url = conf_qanary.get("system_url")
    logger.info("request parameter for Qanary system:\n" + pprint.pformat(data))
    my_response = requests.post(url, data=data)
    logger.info("request as curl:\n" + curlify.to_curl(my_response.request))

    print(f"HTTP response code: {my_response.status_code}")
    logger.info("response of Qanary system:\n" + pprint.pformat(my_response.json()))
    
    return my_response.json()


def sparql_execute_query(logger, question, configuration_directory, sparql_template_filename, connection, graphid):
    logger.info(question)
    replacements = question.get("replacements")
    sparql_query_complete = prepare_sparql_query(logger, configuration_directory+"/"+sparql_template_filename, replacements, graphid)
    logger.info(sparql_query_complete)
    try:
        result = connection.ask(sparql_query_complete)
        logger.info("%s\t%s\t%s" % (question, sparql_template_filename, result))
        return result
    except Exception as e:
        message = "query could not be executed: %s\n%s" % (e, sparql_query_complete)
        logger.error(message)
        raise RuntimeError(message)

    #request_qanary_endpoint(conf_qanary, question)



def evaluate_tests(logger, conf_qanary, configuration_directory, validation_sparql_templates, custom_module, tests):
    """
    evalute all tests defined in the configuration file, here only the request to the Qanary system is executed

    Args:
        logger ([type]): [description]
        conf_qanary ([type]): [description]
        validation_sparql_templates ([type]): [description]
        tests ([type]): [description]

    Returns:
        [type]: [description]
    """
    results = []

    for nr,test in enumerate(tests):
        question = test.get("question")
        logger.info("%d. test: %s" % (nr, pprint.pformat(test)) )
        print("\n%d. test: %s" % (nr, question))

        qanary_response = request_qanary_endpoint_for_question(logger, conf_qanary, question)

        graphid = qanary_response.get("outGraph")
        endpoint = qanary_response.get("endpoint")
        connection = connect_to_triplestore(conf_qanary, endpoint)

        result_per_test = []
        for validation_sparql_template in validation_sparql_templates:
            start = datetime.datetime.now()
            result = evaluate_test(logger, conf_qanary, configuration_directory, test, validation_sparql_template, connection, graphid)
            milliseconds = measure_duration_in_milliseconds(start)

            print(" ... %s (%d ms)" % (result, milliseconds), end='')
            logger.info(" ... %s: %s (%d ms)" % (validation_sparql_template, result, milliseconds))

            result_per_test.append({validation_sparql_template:result})

        
        # at last run the dynamically function to evaluate the created query and determine if it is the correct answer 
        print("\nrun the external custom function: %s " % (custom_module.__name__,), end="") # no newline
        start = datetime.datetime.now()
        custom_result = custom_module.validate(test, logger, conf_qanary, connect_to_triplestore(conf_qanary, endpoint), graphid)
        milliseconds = measure_duration_in_milliseconds(start)
        print("--> %s (%d ms)" % (custom_result, milliseconds) ) # print also the result of the custom evaluation method
        logger.info("custom function: %s (%d ms)" % (custom_result, milliseconds))
        result_per_test.append({"custom_evaluation": custom_result})
        

        results.append({
            "question": question, 
            "graph": graphid,
            "results": result_per_test
        }) #TODO: create result object

    logger.info("\n----------------------------------------\nComplete results:\n%s" % (pprint.pformat(results)))
    
    return results


def evaluate_test(logger, conf_qanary, configuration_directory, test, validation_sparql_template, connection, graphid):
    """
    evaluate one specific SPARQL tests on pre-runned Qanary process

    Args:
        conf_qanary ([type]): [description]
        test ([type]): [description]
        validation_sparql_template ([type]): [description]
        connection ([type]): [description]
        graphid ([type]): [description]

    Returns:
        [type]: [description]
    """
    result = sparql_execute_query(logger, test, configuration_directory, validation_sparql_template, connection, graphid)
    logger.info("question: %s, result: %s, sparql: %s" % (test.get("question"), result, validation_sparql_template))
    return result


def create_data_frame(test_results):
    frame_data = {}

    # init avg column
    headers = get_headers_from_test_results(test_results)
    sum = {}
    for header in headers:
        sum[header] = 0

    # set result column
    for test_result in test_results:
        question = test_result.get("question")
        graph = test_result.get("graph")
        
        frame_data[question] = []
        for result in test_result.get("results"):
            for k in result.keys():
                v = result[k]
                frame_data[question].append(v)
                sum[k] += v
        frame_data[question].append(graph)
    

    avg = [ sum[key]/len(test_results) for key in sum.keys() ]
    avg.append("") # last empty because of graph URIs
    frame_data["average"] = avg

    df = pd.DataFrame(frame_data)
    return df

def get_headers_from_test_results(test_results):
    sparql_query_template_names = []
    for result in test_results[0].get("results"):
        sparql_query_template_names.append(list(result.keys())[0])
    return sparql_query_template_names

def export_to_json(logger, test_results, filename_prefix):
    json_object = json.dumps(test_results, indent = 4) 
    json_filename = filename_prefix + ".json"
    # writing to json file
    with open(json_filename, "w") as outfile: 
        outfile.write(json_object) 
        print("JSON file written to '%s'." % (json_filename,))


def export_to_excel(logger, test_results, filename_prefix, sheet_name):
    """
    export all data to an EXCEL file including a chart

    Args:
        logger ([type]): [description]
        test_results ([type]): [description]
        filename_prefix ([type]): [description]
        sheet_name ([type]): [description]
    """

    rotate_header_degree = 90
    xlsx_filename = f"{filename_prefix}.xlsx"
    
    df = create_data_frame(test_results)
    df = df.transpose()
    df = df[0:] 

    headers = get_headers_from_test_results(test_results)
    headers.append("graph") # will become the additional grpah header
    df.columns = headers # will become the first row
    
    number_of_questions = len(test_results)
    number_of_data_rows = len(df.columns)

    #print(number_of_data_rows, number_of_questions)
    logger.info("final result table:\n" + pprint.pformat(df))

    # Create a Pandas Excel writer using XlsxWriter as the engine.
    writer = pd.ExcelWriter(xlsx_filename, engine='xlsxwriter')

    # Convert the dataframe to an XlsxWriter Excel object.
    df.to_excel(writer, sheet_name=sheet_name, header=True)

    # Get the xlsxwriter objects from the dataframe writer object.
    workbook  = writer.book
    worksheet = writer.sheets[sheet_name]
    
    # Create a chart object.
    chart = workbook.add_chart({'type': 'line'})
    chart.set_size({'width': len(headers) * 50 + 400, 'height': 500})
    chart.set_y_axis({
        'max': 1.0
    })

    first_data_col = 'B'
    last_data_col = chr(ord(first_data_col) + number_of_data_rows - 2)
    chart_col = chr(ord(first_data_col) + number_of_data_rows)
    data_row = 2 # number of first data row
    first_row = data_row

    for test_result in test_results:
        question = test_result.get("question")
        chart.add_series({'values': '=%s!$%s$%d:$%s$%d' % (sheet_name, first_data_col, data_row, last_data_col, data_row), 'name': question})
        data_row += 1

    # add average values
    chart.add_series({
            'values': '=%s!$%s$%d:$%s$%d' % (sheet_name, first_data_col, data_row, last_data_col, data_row), 
            'categories':'=%s!$%s$%d:$%s$%d' % (sheet_name, first_data_col, first_row-1, last_data_col, first_row-1),
            'name': 'avg', 
            'marker': {'type': 'square', 'size': 8.0, 'border': {'color': 'black'}, 'fill':   {'color': 'yellow'} },  
            'line': {'color': 'black', 'width': 4.0} 
    })

    # Insert the chart into the worksheet.
    worksheet.insert_chart('%s1' % (chart_col,), chart)

    # Apply a conditional format to the cell range.
    worksheet.conditional_format('%s%d:%s%d' % (first_data_col, first_row, last_data_col, data_row), {
                                    'type': '2_color_scale',
                                    'criteria': '<',
                                    'value': 0,
                                    'min_color': "#FF9999",
                                    'max_color': "#99FF99"
    })

    # set height of first row
    worksheet.set_row(0, 50)
    # set width of first column
    worksheet.set_column(0, 0, 40)
    worksheet.set_column(1, len(headers), 10)

    # set header format SPARQL columns
    header_format = workbook.add_format({
        'bold': True,
        'text_wrap': True,
        'valign': 'top',
        'align': 'center',
        'size': 8,
        'fg_color': '#CCCCCC',
        'border': 0
    })
    header_format.set_rotation(rotate_header_degree) 

    # Write the column headers with the defined format.
    for col_num, value in enumerate(df.columns.values):
        worksheet.write(0, col_num + 1, value, header_format)

    # set question format 
    question_format = workbook.add_format({
        'bold': False,
        'text_wrap': True,
        'valign': 'top',
        'align': 'left',
        'size': 8,
        'fg_color': '#CCCCFF',
        'border': 0
    })

    # Write the row format
    for my_number in range(0, number_of_questions):
        worksheet.write(my_number+1, 0, test_results[my_number].get("question"), question_format)

    # set question format 
    avg_format = workbook.add_format({
        'bold': True,
        'text_wrap': True,
        'valign': 'top',
        'align': 'left',
        'size': 10,
        'fg_color': '#CCCCFF',
        'border': 0
    })
    worksheet.write(number_of_questions+1, 0, "average:", avg_format)

    # Close the Pandas Excel writer and output the Excel file.
    writer.save()
    print("EXCEL file written to '%s'." % (xlsx_filename,))


def create_printable_string(str):
    return pprint.pformat(str, indent=20)

def determine_if_custom_module_is_callable(logger, custom_module):
    """
    checks if the custom module contains a methode 'validate' with the demanded number of parameters

    Args:
        logger ([type]): [description]
        custom_module ([type]): [description]

    Raises:
        RuntimeError: [description]
        RuntimeError: [description]
    """
    method_name = "validate"

    try:
        custom_module.validate # no call, just checking the name, TODO: improve
        logger.info("Method '%s' found in module '%s'." % (method_name, custom_module.__name__))
    except Exception as e:
        logger.error("Method '%s' NOT found in module '%s'." % (method_name, custom_module.__name__))
        raise RuntimeError("Your custom module '%s' needs to contain a method '%s' " % (custom_module.__name__, method_name))

    if len(inspect.getargspec(custom_module.validate).args) != 5:
        message = "Method '%s' in module '%s' has to contain 5 parameters (typically: 'test', 'logger', 'conf_qanary', 'connection', 'graphid')." % (method_name, custom_module.__name__)
        logger.error(message)
        raise RuntimeError(message)

def measure_duration_in_milliseconds(start):
    return round((datetime.datetime.now() - start).total_seconds() * 1000)

class dummy():
    def validate(test, logger, conf_qanary, connection, graphid):
        return True


def main(logger, sheet_name, configuration_directory, outdir, filename_prefix):
    start = datetime.datetime.now()
    test_configuration_file = configuration_directory + "/qanary-test-definition.json"
    test_configuration = {}

    with open(test_configuration_file) as json_file:
        test_configuration = json.load(json_file)

    conf_qanary = test_configuration.get("qanary")
    conf_validation_sparql_templates = test_configuration.get("validation-sparql-templates")
    conf_tests = test_configuration.get("tests")
    custom_modul_name = test_configuration.get("custom-validation")

    message = """
        current configuration:
            qanary: 
                %s\n
            %d validation SPARQL templates:
                %s\n
            custom module containing validation method:
                %s\n
            %d test questions:
                %s\n
    """ % ( create_printable_string(conf_qanary), 
            len(conf_validation_sparql_templates), create_printable_string(conf_validation_sparql_templates), 
            custom_modul_name,
            len(conf_tests), create_printable_string(conf_tests) 
    ) 
    print(message)
    logger.info(message)

    # import the custom module if defined else use predefined dummy module
    if custom_modul_name != None:
        custom_module = importlib.import_module(configuration_directory + "." + custom_modul_name)
    else:
        custom_module = dummy
    determine_if_custom_module_is_callable(logger, custom_module)

    test_results = evaluate_tests(logger, conf_qanary, configuration_directory, conf_validation_sparql_templates, custom_module, conf_tests)
    runtime_in_seconds = "runtime: %d secs" % (round((datetime.datetime.now() - start).total_seconds()),)
    print("\n"+runtime_in_seconds)
    logger.info(runtime_in_seconds)


    print("\n")
    print("LOG file written to '%s.log'." % (filename_prefix,))
    export_to_json(logger, test_results, filename_prefix)
    export_to_excel(logger, test_results, filename_prefix, sheet_name)


if __name__ == "__main__":
    """ 
        prepare the directories and names
    """
    parser = argparse.ArgumentParser(description='The application executes the tests for your Qanary-based Question Answering system and is creating an XLSX and JSON output into the folder "output".')
    parser.add_argument('-d', '--directory', action='store', default=None,
                        help='required parameter: directory name where the test configuration is available')
    args = parser.parse_args()    

    if args.directory == None:
        parser.print_help()
        raise RuntimeError("Directory not provided.")
    elif not os.path.isdir(args.directory):
        raise RuntimeError("Directory '%s' not available." % (args.directory,))

    outdir = "%s/%s" % (args.directory, outdir)
    if not os.path.isdir(outdir):
        os.mkdir(outdir)

    filename_prefix = "%s/%s_%s" % (outdir, sheet_name,datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S"))

    my_logger = logging.getLogger('qanary-evaluator')
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    my_logger.setLevel(logging.INFO)
    fh = logging.FileHandler('%s.log' % (filename_prefix,))
    fh.setFormatter(formatter)
    fh.setLevel(logging.DEBUG)
    my_logger.addHandler(fh)

    main(my_logger, sheet_name, args.directory, outdir, filename_prefix)
